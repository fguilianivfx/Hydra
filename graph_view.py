"""Rendu interactif du graphe avec QGraphicsView / QGraphicsScene.

Contient :
  * ``SceneNodeItem`` : rectangle arrondi d'une scène (nom + outputs),
    déplaçable horizontalement seulement, avec survol qui met en évidence
    les dépendances directes ;
  * ``EdgeItem`` : arête en courbe de Bézier avec flèche (parent -> enfant) ;
  * ``DependencyGraphView`` : la vue (zoom molette, pan bouton du milieu,
    recentrage, réinitialisation de la disposition).

Aucune dépendance à une librairie de graphe externe.
"""

from __future__ import annotations

import math
from collections import defaultdict

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

import graph_model as gm

# --- Palette (fond sombre, cartes pastel) -----------------------------------
COL_BG = QColor("#14171b")
COL_TITLE = QColor("#1f2529")          # texte foncé, lisible sur pastel clair
COL_META_START = QColor("#6a4d0e")     # ambre foncé pour « queried scene »
COL_ROW_LABEL = QColor("#7b8593")
COL_ROW_GUIDE = QColor(255, 255, 255, 12)

# Fond / bordure du nœud selon son statut de propagation (tons pastel).
COL_OK_FILL = QColor("#c2e7cf")        # vert pastel
COL_OK_BORDER = QColor("#8ccaa4")
COL_STALE_FILL = QColor("#f3c1ba")     # rouge/rose pastel
COL_STALE_BORDER = QColor("#e0988d")
COL_INHERITED_FILL = QColor("#f3edad") # jaune pastel
COL_INHERITED_BORDER = QColor("#d4c95f")
COL_START_BORDER = QColor("#d9a93f")   # or, accent de la scène interrogée

# Couleur du texte d'un output/input selon sa fraîcheur (foncé sur pastel).
COL_ASSET_OK = QColor("#1c7a44")       # vert foncé
COL_ASSET_STALE = QColor("#b4392c")    # rouge foncé

COL_EDGE_DISABLED = QColor(130, 138, 150, 120)   # lien désactivé (pointillés)

# Poignée de ligne (à gauche) + séparateur asset/shot.
COL_HEADER_BG = QColor("#20242b")
COL_HEADER_HOVER = QColor("#2c323b")
COL_HEADER_BORDER = QColor("#3a414c")
COL_SEPARATOR = QColor("#727e8f")

# --- Géométrie --------------------------------------------------------------
# Largeur calée sur le pire cas d'une ligne d'output : 20 caractères + « ... »
# suivis de « ⚠ (v005 → v007) ». En dessous, un nom long se faisait ré-élider
# et la règle des 20 caractères n'était pas tenue.
NODE_W = 252.0
PAD = 11.0
COL_W = NODE_W + 52.0
EDGE_HIT_WIDTH = 12.0      # largeur de la zone cliquable d'un lien
HEADER_H = 26.0            # hauteur de la poignée de ligne
SEP_EXTRA = 34.0          # espace supplémentaire autour du séparateur
GUIDE_DX = 18.0
ROW_GAP = 74.0
MARGIN_LEFT = 156.0
MARGIN_TOP = 46.0
MIN_ROW_H = 70.0

FADE_OPACITY = 0.16

# Zoom : pas de la molette et bornes absolues. La borne basse est encore
# abaissée pour les très grands graphes (voir _zoom_bounds).
ZOOM_STEP = 1.15
ZOOM_MIN = 0.02
ZOOM_MAX = 12.0


# Longueur max d'un nom d'output dans un rectangle, avant troncature.
NODE_NAME_MAX = 20


def abbreviate_node(name):
    """20 premiers caractères d'un node_name, suivis de « ... » si tronqué."""
    if not name:
        return ""
    if len(name) <= NODE_NAME_MAX:
        return name
    return name[:NODE_NAME_MAX] + "..."


def _vfmt(version):
    """Formate une version : 19 -> 'v019' ; None -> 'v?'."""
    return f"v{version:03d}" if version is not None else "v?"


def _fmt_tag(fmt):
    """Suffixe de format pour les info-bulles : « abc » -> ' [abc]'."""
    return f" [{fmt}]" if fmt else ""


def _vstate(current, latest):
    """« (vXXX) » si à jour, « (vXXX → vYYY) » si une version plus récente existe."""
    if current is not None and latest is not None and current < latest:
        return f"({_vfmt(current)} → {_vfmt(latest)})"
    return f"({_vfmt(current)})"


def _fit_output_line(name, suffix, fm, max_w):
    """Assemble « nom + suffixe » en gardant le suffixe (versions) visible."""
    if fm.horizontalAdvance(name + suffix) <= max_w:
        return name + suffix
    name_max = max(10.0, max_w - fm.horizontalAdvance(suffix))
    return fm.elidedText(name, Qt.ElideRight, name_max) + suffix


def _draw_text(painter, x, y, text, color):
    painter.setPen(QPen(color))
    painter.drawText(QPointF(x, y), text)


def _wrap(words, sep, fm, max_w):
    """Retour à la ligne glouton d'une liste de mots joints par ``sep``."""
    lines = []
    cur = ""
    for w in words:
        trial = w if not cur else cur + sep + w
        if fm.horizontalAdvance(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


# ---------------------------------------------------------------------------
# Arête
# ---------------------------------------------------------------------------

class EdgeItem(QGraphicsPathItem):
    """Courbe de Bézier orientée : du parent (haut) vers l'enfant (bas)."""

    STATE_DEFAULT = 0
    STATE_HILITE = 1
    STATE_FADED = 2

    def __init__(self, src_item, dst_item, top_key, bottom_key, view=None,
                 carries_stale=False):
        super().__init__()
        self.src = src_item          # parent (row du haut)
        self.dst = dst_item          # enfant (row du bas)
        self.top_key = top_key
        self.bottom_key = bottom_key
        self.key = (top_key, bottom_key)
        self._view = view
        # Ce lien transporte-t-il au moins un asset supplanté ?
        self.carries_stale = carries_stale
        self._arrow_size = 9.0
        self._state = self.STATE_DEFAULT
        self.disabled = False        # désactivation temporaire (en mémoire)
        self.selected = False
        # Cintrage horizontal des arêtes qui sautent des lignes, pour rester
        # visibles quand les nœuds sont empilés dans la même colonne.
        self._row_span = abs(dst_item.node.row - src_item.node.row)
        self._bow_dir = 1.0 if (hash((top_key, bottom_key)) % 2 == 0) else -1.0
        self.setZValue(-1)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)
        self.refresh_appearance()
        self.update_path()

    def details(self):
        """[(label, version, dernière version, format)] transitant par le lien."""
        result = getattr(self._view, "_result", None) if self._view else None
        if result is None:
            return []
        return result.edge_details.get(self.key, [])

    # --- couleur du lien ----------------------------------------------------
    def _base_color(self):
        """Couleur du lien : d'où vient l'obsolescence de l'enfant ?

        * enfant **vert** -> lien vert ;
        * enfant obsolète (rouge ou jaune) :
            - **rouge** si CE lien transporte un asset supplanté (il est la
              cause directe) ;
            - sinon **jaune** si le parent est lui-même obsolète (l'obsolescence
              arrive par ce lien, mais héritée) ;
            - sinon **vert** (ce lien est sain, la cause est ailleurs).
        """
        if self.dst.node.status == "ok":
            return COL_OK_BORDER
        if self.carries_stale:
            return COL_STALE_BORDER
        if self.src.node.status != "ok":
            return COL_INHERITED_BORDER
        return COL_OK_BORDER

    def refresh_appearance(self):
        """Recalcule stylo et infobulle selon statut/état courant."""
        color = QColor(self._base_color())
        width = 1.8
        style = Qt.SolidLine

        if self.disabled:
            color = QColor(COL_EDGE_DISABLED)
            style = Qt.DashLine
            width = 1.4
        elif self._state == self.STATE_HILITE:
            color = color.lighter(125)
            width = 2.8
        elif self._state == self.STATE_FADED:
            color.setAlpha(45)
            width = 1.2

        if self.selected:
            width = max(width, 3.2)
            if not self.disabled:
                color = color.lighter(135)

        pen = QPen(color, width)
        pen.setStyle(style)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        self._pen = pen
        self.setPen(pen)
        carried = ("carries an outdated asset" if self.carries_stale
                   else "all assets up to date")
        lines = [self.src.node.display_name,
                 f"→ {self.dst.node.display_name}",
                 f"This link: {carried}"]
        details = self.details()
        if details:
            lines.append(f"Assets through this link ({len(details)}):")
            for label, cur, latest, fmt in details:
                lines.append(f"  {label}{_fmt_tag(fmt)} {_vstate(cur, latest)}")
        lines.append("DISABLED — right-click to re-enable" if self.disabled
                     else "Click: details · Right-click: disable this link")
        self.setToolTip("\n".join(lines))
        self.update()

    def set_state(self, state):
        if state != self._state:
            self._state = state
            self.setZValue(1 if state == self.STATE_HILITE else -1)
            self.refresh_appearance()

    def set_selected(self, selected):
        if selected != self.selected:
            self.selected = selected
            self.setZValue(2 if selected else -1)
            self.refresh_appearance()

    def set_disabled(self, disabled):
        if disabled != self.disabled:
            self.disabled = disabled
            self.refresh_appearance()

    def update_path(self):
        p = self.src.bottom_anchor()
        c = self.dst.top_anchor()
        dx = c.x() - p.x()
        vgap = max(28.0, abs(c.y() - p.y()) * 0.45)
        # On ne cintre que les arêtes multi-lignes dont les extrémités sont
        # (presque) alignées verticalement ; sinon la courbe reste directe.
        bow = 0.0
        if self._row_span >= 2 and abs(dx) < NODE_W * 0.6:
            bow = min(90.0, 26.0 * (self._row_span - 1)) * self._bow_dir
        path = QPainterPath(p)
        path.cubicTo(QPointF(p.x() + bow, p.y() + vgap),
                     QPointF(c.x() + bow, c.y() - vgap), c)
        self.setPath(path)

    def boundingRect(self):
        extra = self._arrow_size + self._pen.widthF() + EDGE_HIT_WIDTH
        return super().boundingRect().adjusted(-extra, -extra, extra, extra)

    def shape(self):
        """Zone cliquable élargie : une courbe fine est difficile à viser."""
        stroker = QPainterPathStroker()
        stroker.setWidth(EDGE_HIT_WIDTH)
        return stroker.createStroke(self.path())

    # --- interactions -------------------------------------------------------
    def hoverEnterEvent(self, event):
        if self._view is not None:
            self._view._on_edge_hover(self)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if self._view is not None:
            self._view._on_edge_unhover(self)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if self._view is None:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.RightButton:
            self._view.toggle_edge_disabled(self)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._view.select_edge(self)
            event.accept()
            return
        super().mousePressEvent(event)

    def _arrow_polygon(self):
        path = self.path()
        if path.isEmpty():
            return QPolygonF()
        tip = path.pointAtPercent(1.0)
        back = path.pointAtPercent(0.88)
        v = tip - back
        length = math.hypot(v.x(), v.y()) or 1.0
        ux, uy = v.x() / length, v.y() / length
        px, py = -uy, ux
        size = self._arrow_size
        base = QPointF(tip.x() - ux * size, tip.y() - uy * size)
        left = QPointF(base.x() + px * size * 0.55, base.y() + py * size * 0.55)
        right = QPointF(base.x() - px * size * 0.55, base.y() - py * size * 0.55)
        return QPolygonF([tip, left, right])

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(self._pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())
        # Lien désactivé : trait pointillé sans flèche pleine.
        if not self.disabled:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(self._pen.color()))
            painter.drawPolygon(self._arrow_polygon())


# ---------------------------------------------------------------------------
# Nœud
# ---------------------------------------------------------------------------

class SceneNodeItem(QGraphicsObject):
    """Rectangle d'une scène : nom en haut, liste des nodes dessous."""

    hovered = Signal(object)      # émet self
    unhovered = Signal(object)    # émet self

    def __init__(self, node):
        super().__init__()
        self.node = node          # graph_model.SceneNode
        self._row_y = 0.0
        self._edges = []          # arêtes connectées (pour mise à jour)
        self._height = MIN_ROW_H

        self._title_font = QFont()
        self._title_font.setBold(True)
        self._title_font.setPointSizeF(10.0)
        self._sub_font = QFont()
        self._sub_font.setPointSizeF(8.2)
        self._meta_font = QFont()
        self._meta_font.setPointSizeF(8.0)
        self._meta_font.setItalic(True)

        self._title_lines = []
        self._meta_lines = []       # list[(texte, couleur)]
        self._output_lines = []     # list[(texte, couleur)] : un asset/ligne

        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(0)
        self.setToolTip(self._tooltip_text())
        self._relayout()

    # --- géométrie / contenu ------------------------------------------------
    _version_state = staticmethod(_vstate)

    def _tooltip_text(self):
        n = self.node
        parts = [n.display_name]
        parts.append("Artist: "
                     + (", ".join(n.artists) if n.artists else "unknown"))
        if n.inputs:
            parts.append("Inputs:")
            for label, cur, latest, fmt in n.inputs:
                parts.append(f"  {label}{_fmt_tag(fmt)} "
                             f"{self._version_state(cur, latest)}")
        if n.outputs:
            parts.append("Outputs:")
            for name, latest, fmt in n.outputs:
                parts.append(
                    f"  {name}{_fmt_tag(fmt)} "
                    f"{self._version_state(n.version, latest)}")
        parts.append({
            "ok": "up to date",
            "stale": "outdated (stale input)",
            "inherited": "outdated by inheritance",
        }.get(n.status, n.status))
        return "\n".join(parts)

    def _relayout(self):
        self.prepareGeometryChange()
        n = self.node
        inner_w = NODE_W - 2 * PAD

        fm_title = QFontMetricsF(self._title_font)
        fm_out = QFontMetricsF(self._sub_font)
        fm_meta = QFontMetricsF(self._meta_font)

        # Titre (nom canonique) sur 1-2 lignes, avec élision si nécessaire.
        title_lines = _wrap(n.display_name.split("_"), "_", fm_title, inner_w)
        if len(title_lines) > 2:
            title_lines = [title_lines[0],
                           "_".join(title_lines[1:])]
        self._title_lines = [
            fm_title.elidedText(t, Qt.ElideRight, inner_w) for t in title_lines
        ]

        # Ligne méta : uniquement « scène de départ » (plus de badge version).
        self._meta_lines = []
        if n.is_start:
            self._meta_lines.append(("— queried scene —", COL_META_START))

        # Un output par ligne, coloré selon sa fraîcheur :
        #   à jour  -> vert :  "name (v001)"
        #   périmé  -> rouge : "name ⚠ (v001 → v002)"
        self._output_lines = []
        for name, latest, _fmt in n.outputs:
            v = n.version
            if v is not None and latest is not None and v < latest:
                suffix = f"  ⚠ ({_vfmt(v)} → {_vfmt(latest)})"
                color = COL_ASSET_STALE
            else:
                suffix = f"  ({_vfmt(v)})"
                color = COL_ASSET_OK
            text = _fit_output_line(abbreviate_node(name), suffix,
                                    fm_out, inner_w)
            self._output_lines.append((text, color))

        # Hauteur totale.
        h = PAD
        h += len(self._title_lines) * fm_title.height()
        if self._meta_lines:
            h += 3 + len(self._meta_lines) * fm_meta.height()
        if self._output_lines:
            h += 6 + len(self._output_lines) * fm_out.height()
        h += PAD
        self._height = max(MIN_ROW_H, h)

    def boundingRect(self):
        return QRectF(0.0, 0.0, NODE_W, self._height)

    def height(self):
        return self._height

    # --- ancrage des arêtes -------------------------------------------------
    def top_anchor(self):
        r = self.boundingRect()
        return self.mapToScene(QPointF(r.center().x(), r.top()))

    def bottom_anchor(self):
        r = self.boundingRect()
        return self.mapToScene(QPointF(r.center().x(), r.bottom()))

    def add_edge(self, edge):
        self._edges.append(edge)

    def refresh_status(self):
        """Le statut du nœud a changé : infobulle + repeinture."""
        self.setToolTip(self._tooltip_text())
        self.update()

    def set_row_y(self, y):
        self._row_y = y

    # --- déplacement horizontal uniquement ---------------------------------
    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            # Verrouille le Y sur la ligne de la tâche.
            return QPointF(value.x(), self._row_y)
        if change == QGraphicsItem.ItemPositionHasChanged:
            for edge in self._edges:
                edge.update_path()
        return super().itemChange(change, value)

    # --- survol -------------------------------------------------------------
    def hoverEnterEvent(self, event):
        self.hovered.emit(self)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.unhovered.emit(self)
        super().hoverLeaveEvent(event)

    # --- peinture -----------------------------------------------------------
    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        n = self.node
        rect = self.boundingRect().adjusted(0.5, 0.5, -0.5, -0.5)

        if n.status == "stale":
            fill, border = COL_STALE_FILL, COL_STALE_BORDER
        elif n.status == "inherited":
            fill, border = COL_INHERITED_FILL, COL_INHERITED_BORDER
        else:
            fill, border = COL_OK_FILL, COL_OK_BORDER

        if n.is_start:
            pen = QPen(COL_START_BORDER, 2.6)
        elif self.isSelected():
            pen = QPen(border.lighter(135), 2.2)
        else:
            pen = QPen(border, 1.5)

        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(rect, 9.0, 9.0)

        inner_w = NODE_W - 2 * PAD
        x = PAD
        y = PAD

        fm_title = QFontMetricsF(self._title_font)
        painter.setFont(self._title_font)
        for line in self._title_lines:
            y += fm_title.ascent()
            _draw_text(painter, x, y, line, COL_TITLE)
            y += fm_title.descent() + fm_title.leading()

        if self._meta_lines:
            y += 3
            fm_meta = QFontMetricsF(self._meta_font)
            painter.setFont(self._meta_font)
            for text, color in self._meta_lines:
                y += fm_meta.ascent()
                _draw_text(painter, x, y, text, color)
                y += fm_meta.descent() + fm_meta.leading()

        if self._output_lines:
            y += 6
            fm_out = QFontMetricsF(self._sub_font)
            painter.setFont(self._sub_font)
            for text, color in self._output_lines:
                y += fm_out.ascent()
                _draw_text(painter, x, y, text, color)
                y += fm_out.descent() + fm_out.leading()


# ---------------------------------------------------------------------------
# Ligne de tâche + poignée déplaçable
# ---------------------------------------------------------------------------

class _RowInfo:
    """Une ligne de tâche : sa poignée, ses nœuds, son guide."""

    __slots__ = ("task", "level", "nodes", "header", "guide", "top")

    def __init__(self, task, level, nodes):
        self.task = task
        self.level = level
        self.nodes = nodes
        self.header = None
        self.guide = None
        self.top = 0.0


class RowHeaderItem(QGraphicsObject):
    """Poignée à gauche d'une ligne : se déplace verticalement pour réordonner
    les lignes de tâche (le X reste verrouillé)."""

    WIDTH = MARGIN_LEFT - 26.0

    def __init__(self, text, view):
        super().__init__()
        self._text = text
        self._view = view
        self._x = 12.0
        self._hover = False
        self.row = None                # _RowInfo associé
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip("Drag to reorder task rows")
        self.setZValue(5)

    def boundingRect(self):
        return QRectF(0.0, 0.0, self.WIDTH, HEADER_H)

    def set_top(self, y):
        self.setPos(QPointF(self._x, y))

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            return QPointF(self._x, value.y())          # verrouille le X
        if change == QGraphicsItem.ItemPositionHasChanged:
            self._view._row_header_moved(self)
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        self.setZValue(20)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.setZValue(5)
        super().mouseReleaseEvent(event)
        self._view._row_header_released(self)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.boundingRect().adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(COL_HEADER_BORDER, 1.0))
        painter.setBrush(QBrush(COL_HEADER_HOVER if self._hover
                                else COL_HEADER_BG))
        painter.drawRoundedRect(rect, 5.0, 5.0)
        # petite poignée (deux colonnes de points)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(COL_ROW_LABEL))
        cy = rect.center().y()
        for i in range(2):
            for j in range(3):
                painter.drawEllipse(QPointF(8.0 + i * 4.0, cy - 4.0 + j * 4.0),
                                    1.1, 1.1)
        font = QFont()
        font.setBold(True)
        font.setPointSizeF(9.0)
        painter.setFont(font)
        painter.setPen(QPen(COL_ROW_LABEL))
        painter.drawText(rect.adjusted(22.0, 0.0, -4.0, 0.0),
                         Qt.AlignVCenter | Qt.AlignLeft, self._text)


# ---------------------------------------------------------------------------
# Vue
# ---------------------------------------------------------------------------

class DependencyGraphView(QGraphicsView):
    """QGraphicsView avec zoom molette, pan bouton du milieu, survol."""

    # Émis au clic sur un lien : dict décrivant le lien sélectionné.
    edge_selected = Signal(object)
    # Émis quand des liens sont désactivés/réactivés (nb de liens désactivés).
    links_changed = Signal(int)
    # Émis quand des nœuds sont masqués/réaffichés (nb de nœuds masqués).
    hidden_nodes_changed = Signal(int)
    # Émis quand les filtres de liens (même tâche, formats) sont remis à zéro,
    # pour que l'interface décoche ses cases.
    filters_cleared = Signal()
    # Émis après un nouveau graphe : liste des formats d'assets rencontrés.
    formats_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QBrush(COL_BG))
        self.setScene(self._scene)

        self.setRenderHint(QPainter.Antialiasing, True)
        self.setRenderHint(QPainter.TextAntialiasing, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._node_items = {}       # key -> SceneNodeItem
        self._edges = []            # list[EdgeItem]
        self._rows = []             # list[_RowInfo] dans l'ordre d'affichage
        self._initial_rows = []     # ordre initial (pour reset)
        self._initial_pos = {}      # key -> QPointF (pour reset layout)
        self._separator = None      # trait séparateur asset/shot
        self._sep_label = None
        self._reflowing = False
        self._zoom = 1.0
        self._result = None         # graph_model.GraphResult courant
        self._selected_edge = None
        # Liens désactivés temporairement : en mémoire uniquement, remis à
        # zéro à chaque nouveau graphe, jamais écrits en base.
        self._disabled_edges = set()
        # Filtres de liens (mêmes garanties : temporaires, en mémoire).
        self._mute_same_task = False
        self._muted_formats = set()
        # Afficher les nœuds devenus inaccessibles (reliés uniquement par des
        # liens désactivés) ?
        self._show_disconnected = True

        self._panning = False
        self._pan_last = None

    # --- construction -------------------------------------------------------
    def clear_graph(self):
        self._scene.clear()
        self._node_items.clear()
        self._edges.clear()
        self._rows = []
        self._initial_rows = []
        self._initial_pos.clear()
        self._separator = None
        self._sep_label = None
        self._result = None
        self._selected_edge = None
        # Les désactivations sont perdues dès que le graphe change.
        self._disabled_edges = set()

    def set_graph(self, result):
        """Affiche un graph_model.GraphResult."""
        self.clear_graph()
        if not result.nodes:
            self.formats_changed.emit([])
            self.links_changed.emit(0)
            return
        self._result = result

        # 1) items nœud.
        for key, node in result.nodes.items():
            item = SceneNodeItem(node)
            item.hovered.connect(self._on_node_hover)
            item.unhovered.connect(self._on_node_unhover)
            self._node_items[key] = item
            self._scene.addItem(item)
            item.setPos(QPointF(MARGIN_LEFT + node.col * COL_W, MARGIN_TOP))

        # 2) lignes (une par tâche, dans l'ordre du modèle) + poignées + guides.
        by_row = {}
        for item in self._node_items.values():
            by_row.setdefault(item.node.row, []).append(item)
        for r in sorted(by_row):
            info = _RowInfo(result.row_tasks.get(r, "?"),
                            result.row_levels.get(r, "other"),
                            by_row[r])
            header = RowHeaderItem(info.task, self)
            header.row = info
            info.header = header
            self._scene.addItem(header)
            guide = self._scene.addLine(QLineF(), QPen(COL_ROW_GUIDE, 1.0))
            guide.setZValue(-3)
            info.guide = guide
            self._rows.append(info)
        self._initial_rows = list(self._rows)

        # 3) séparateur asset / shot.
        pen = QPen(COL_SEPARATOR, 1.4)
        pen.setStyle(Qt.DashLine)
        self._separator = self._scene.addLine(QLineF(), pen)
        self._separator.setZValue(-2)
        self._sep_label = QGraphicsSimpleTextItem("assets  /  shot")
        f = QFont()
        f.setPointSizeF(8.0)
        self._sep_label.setFont(f)
        self._sep_label.setBrush(QBrush(COL_SEPARATOR))
        self._sep_label.setZValue(-2)
        self._scene.addItem(self._sep_label)

        # 4) arêtes.
        for top_key, bottom_key in result.edges:
            src = self._node_items.get(top_key)
            dst = self._node_items.get(bottom_key)
            if src is None or dst is None:
                continue
            edge = EdgeItem(src, dst, top_key, bottom_key, view=self,
                            carries_stale=(top_key, bottom_key)
                            in result.stale_edges)
            self._scene.addItem(edge)
            self._edges.append(edge)
            src.add_edge(edge)
            dst.add_edge(edge)

        # 5) placement.
        self._reflow(record_initial=True)
        self.reset_view()

        # 6) les filtres actifs s'appliquent au nouveau graphe (les formats
        # absents de celui-ci sont simplement sans effet).
        self.formats_changed.emit(list(result.formats))
        if self._mute_same_task or self._muted_formats:
            self._apply_status_recompute()
        self.links_changed.emit(len(self._effective_disabled()))

    # --- liens : sélection et désactivation temporaire ----------------------
    def select_edge(self, edge):
        """Sélectionne un lien et publie son détail (inputs/outputs)."""
        if self._selected_edge is not None and self._selected_edge is not edge:
            self._selected_edge.set_selected(False)
        edge.set_selected(True)
        self._selected_edge = edge
        self.edge_selected.emit(self.edge_info(edge))

    def clear_edge_selection(self):
        if self._selected_edge is not None:
            self._selected_edge.set_selected(False)
            self._selected_edge = None

    def edge_info(self, edge):
        """Décrit un lien : scènes reliées et assets qui y transitent."""
        details = []
        if self._result is not None:
            details = self._result.edge_details.get(edge.key, [])
        return {
            "key": edge.key,
            "parent": edge.src.node.display_name,
            "child": edge.dst.node.display_name,
            "parent_status": edge.src.node.status,
            "child_status": edge.dst.node.status,
            "disabled": edge.disabled,
            "carries_stale": edge.carries_stale,
            # Assets exportés par le parent et importés par l'enfant.
            "assets": list(details),
            "parent_outputs": list(edge.src.node.outputs),
            "parent_version": edge.src.node.version,
            "child_outputs": list(edge.dst.node.outputs),
            "child_version": edge.dst.node.version,
        }

    def toggle_edge_disabled(self, edge):
        """Active/désactive un lien (en mémoire) et recalcule les statuts."""
        if edge.key in self._disabled_edges:
            self._disabled_edges.discard(edge.key)
        else:
            self._disabled_edges.add(edge.key)
        self._apply_status_recompute()
        if edge is self._selected_edge:
            self.edge_selected.emit(self.edge_info(edge))
        self.links_changed.emit(len(self._effective_disabled()))

    def enable_all_links(self):
        """Réactive tous les liens : clic droit **et** filtres."""
        if not self._effective_disabled():
            return
        self._disabled_edges.clear()
        had_filters = self._mute_same_task or self._muted_formats
        self._mute_same_task = False
        self._muted_formats = set()
        self._apply_status_recompute()
        if had_filters:
            self.filters_cleared.emit()
        self.links_changed.emit(0)

    def disabled_link_count(self):
        return len(self._effective_disabled())

    # --- filtres de liens ---------------------------------------------------
    def set_mute_same_task(self, on):
        """Coupe les liens entre deux scènes portant la **même tâche**."""
        on = bool(on)
        if on == self._mute_same_task:
            return
        self._mute_same_task = on
        self._apply_status_recompute()
        self.links_changed.emit(len(self._effective_disabled()))

    def mute_same_task(self):
        return self._mute_same_task

    def set_muted_formats(self, formats):
        """Coupe les liens transportant l'un de ces formats (abc, exr…).

        Primitive interne : l'interface expose le **complément**, plus parlant
        (« Show formats »), via ``set_shown_formats``.
        """
        wanted = {str(f).lower() for f in formats}
        if wanted == self._muted_formats:
            return
        self._muted_formats = wanted
        self._apply_status_recompute()
        self.links_changed.emit(len(self._effective_disabled()))

    def muted_formats(self):
        return set(self._muted_formats)

    def set_shown_formats(self, formats):
        """Ne garde que les liens transportant l'un de ces formats.

        Un format du graphe absent de la liste voit ses liens coupés ; un
        format inconnu du graphe est simplement sans effet.
        """
        shown = {str(f).lower() for f in formats}
        self.set_muted_formats(set(self.graph_formats()) - shown)

    def shown_formats(self):
        return set(self.graph_formats()) - self._muted_formats

    def graph_formats(self):
        """Formats d'assets présents dans le graphe courant."""
        return list(self._result.formats) if self._result else []

    def _filtered_edges(self):
        """Liens coupés par les filtres (même tâche, formats)."""
        if self._result is None:
            return set()
        cut = set()
        if self._mute_same_task:
            for top, bottom in self._result.edges:
                src = self._result.nodes.get(top)
                dst = self._result.nodes.get(bottom)
                if src is None or dst is None:
                    continue
                if gm.canon_task(src.task_name) == gm.canon_task(dst.task_name):
                    cut.add((top, bottom))
        if self._muted_formats:
            for edge, formats in self._result.edge_formats.items():
                if formats & self._muted_formats:
                    cut.add(edge)
        return cut

    def _effective_disabled(self):
        """Liens réellement inactifs : clic droit **plus** filtres."""
        return self._disabled_edges | self._filtered_edges()

    def _apply_status_recompute(self):
        """Recalcule les statuts (rien n'est écrit en base) et rafraîchit."""
        if self._result is None:
            return
        disabled = self._effective_disabled()
        gm.recompute_status(self._result, disabled)
        for edge in self._edges:
            edge.set_disabled(edge.key in disabled)
        for item in self._node_items.values():
            item.refresh_status()
        for edge in self._edges:
            edge.refresh_appearance()
        self._update_visibility()

    # --- visibilité des nœuds coupés du graphe ------------------------------
    def set_show_disconnected(self, show):
        """Affiche ou masque les nœuds reliés uniquement par des liens désactivés."""
        show = bool(show)
        if show == self._show_disconnected:
            return
        self._show_disconnected = show
        self._update_visibility()

    def show_disconnected(self):
        return self._show_disconnected

    def _reachable_keys(self):
        """Nœuds ayant encore un chemin ACTIF jusqu'à la scène interrogée.

        On remonte depuis S0 en suivant les liens non désactivés (enfant ->
        parent). Les nœuds hors de cet ensemble ne sont plus reliés au graphe
        que par des liens désactivés.
        """
        if self._result is None:
            return set()
        parents_of = defaultdict(list)
        for edge in self._edges:
            if not edge.disabled:
                parents_of[edge.bottom_key].append(edge.top_key)
        start = self._result.start_key
        seen = {start}
        stack = [start]
        while stack:
            key = stack.pop()
            for parent in parents_of.get(key, ()):
                if parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        return seen

    def hidden_node_count(self):
        if self._show_disconnected or self._result is None:
            return 0
        return len(self._node_items) - len(self._reachable_keys())

    def _compact_columns(self):
        """Resserre chaque ligne : les nœuds masqués ne laissent plus de trous.

        ``node.col`` est l'indice du nœud **dans sa ligne** : masquer des
        branches y creuse des colonnes vides et étale le graphe sur toute sa
        largeur d'origine. On renumérote donc les nœuds visibles de chaque
        ligne, en conservant leur ordre gauche-droite — celui calculé par
        barycentre pour limiter les croisements.

        Le classement se fait toujours sur ``node.col`` (jamais sur la
        position courante) : la compaction est idempotente, et un nœud
        déplacé à la main revient sur sa colonne quand l'affichage change.
        """
        for row in self._rows:
            visible = sorted((it for it in row.nodes if it.isVisible()),
                             key=lambda it: it.node.col)
            # Les masqués sont rangés à la suite : ils ne se superposent pas
            # aux visibles, et retrouvent leur place à la compaction suivante.
            hidden = sorted((it for it in row.nodes if not it.isVisible()),
                            key=lambda it: it.node.col)
            for i, it in enumerate(visible + hidden):
                it.setPos(QPointF(MARGIN_LEFT + i * COL_W, it.y()))

    def _update_visibility(self):
        """Applique l'option d'affichage et replace ce qui reste."""
        if self._result is None:
            return
        if self._show_disconnected:
            visible = set(self._node_items)
        else:
            visible = self._reachable_keys()
        for key, item in self._node_items.items():
            item.setVisible(key in visible)
        for edge in self._edges:
            edge.setVisible(edge.top_key in visible
                            and edge.bottom_key in visible)
        # La disposition n'est PAS recalculée ici : masquer des nœuds laisse
        # les autres en place, et c'est le bouton « Redraw layout » qui
        # resserre le graphe quand on le demande.
        self._reflow()
        self.hidden_nodes_changed.emit(len(self._node_items) - len(visible))

    def redraw_layout(self):
        """Resserre le graphe sur ce qui est affiché et recadre la vue."""
        if self._result is None:
            return
        self._compact_columns()
        self._reflow()
        self.reset_view()

    # --- survol d'un lien ---------------------------------------------------
    def _on_edge_hover(self, edge):
        edge.set_state(EdgeItem.STATE_HILITE)

    def _on_edge_unhover(self, edge):
        edge.set_state(EdgeItem.STATE_DEFAULT)

    # --- disposition des lignes --------------------------------------------
    def _scene_right(self):
        # Sur la position réelle et non sur node.col : après compaction, les
        # colonnes du modèle ne correspondent plus à ce qui est dessiné.
        max_x = max((it.x() for it in self._node_items.values()
                     if it.isVisible()), default=MARGIN_LEFT - COL_W)
        return max_x + COL_W

    @staticmethod
    def _visible_nodes(row):
        return [it for it in row.nodes if it.isVisible()]

    def _separator_after_index(self, rows=None):
        """Indice de la dernière ligne « asset » suivie d'au moins une autre."""
        rows = self._rows if rows is None else rows
        asset_idx = [i for i, row in enumerate(rows) if row.level == "asset"]
        if not asset_idx:
            return None
        last = max(asset_idx)
        return last if last < len(rows) - 1 else None

    def _reflow(self, record_initial=False):
        """Recalcule les Y de chaque ligne et repositionne tout.

        Les lignes dont tous les nœuds sont masqués sont escamotées (pas de
        bande vide au milieu du graphe).
        """
        if not self._rows:
            return
        self._reflowing = True
        right = self._scene_right()
        # Lignes réellement affichées ; les autres sont entièrement cachées.
        shown = []
        for row in self._rows:
            visible = self._visible_nodes(row)
            has_content = bool(visible)
            row.header.setVisible(has_content)
            row.guide.setVisible(has_content)
            if has_content:
                shown.append(row)
        sep_after = self._separator_after_index(shown)
        y = MARGIN_TOP
        for i, row in enumerate(shown):
            row.top = y
            visible = self._visible_nodes(row)
            height = max((it.height() for it in visible), default=MIN_ROW_H)
            row.guide.setLine(MARGIN_LEFT - GUIDE_DX, y - ROW_GAP * 0.4,
                              right, y - ROW_GAP * 0.4)
            row.header.set_top(y)
            for it in row.nodes:
                it.set_row_y(y)
                it.setPos(QPointF(it.x(), y))
                if record_initial:
                    self._initial_pos[it.node.key] = QPointF(it.x(), y)
            y += height + ROW_GAP
            if sep_after is not None and i == sep_after:
                sep_y = y - ROW_GAP * 0.5
                self._separator.setLine(MARGIN_LEFT - GUIDE_DX, sep_y,
                                        right, sep_y)
                self._separator.setVisible(True)
                self._sep_label.setPos(QPointF(14, sep_y - 15))
                self._sep_label.setVisible(True)
                y += SEP_EXTRA
        if sep_after is None:
            self._separator.setVisible(False)
            self._sep_label.setVisible(False)
        for edge in self._edges:
            edge.update_path()
        self._reflowing = False
        self._update_scene_rect()

    def _visible_bounding_rect(self):
        """Emprise des seuls items visibles (itemsBoundingRect inclut les
        items masqués, ce qui laisserait du vide autour du graphe)."""
        rect = QRectF()
        for item in self._scene.items():
            if item.isVisible():
                rect = rect.united(item.sceneBoundingRect())
        return rect

    def _update_scene_rect(self):
        margin = 80.0
        rect = self._visible_bounding_rect()
        if rect.isNull():
            return
        self._scene.setSceneRect(
            rect.adjusted(-margin, -margin, margin, margin))

    def _row_header_moved(self, header):
        """Suivi live : la ligne (nœuds + guide) suit la poignée pendant le drag."""
        if self._reflowing or header.row is None:
            return
        y = header.y()
        row = header.row
        right = self._scene_right()
        row.guide.setLine(MARGIN_LEFT - GUIDE_DX, y - ROW_GAP * 0.4,
                          right, y - ROW_GAP * 0.4)
        for it in row.nodes:
            it.set_row_y(y)
            it.setPos(QPointF(it.x(), y))

    def _row_header_released(self, header):
        """Réordonne les lignes selon la position verticale des poignées."""
        self._rows.sort(key=lambda row: row.header.y())
        self._reflow()

    # --- survol : mise en évidence des voisins directs ----------------------
    def _on_node_hover(self, item):
        key = item.node.key
        neighbors = {key}
        hl_edges = set()
        for edge in self._edges:
            if edge.top_key == key or edge.bottom_key == key:
                hl_edges.add(edge)
                neighbors.add(edge.top_key)
                neighbors.add(edge.bottom_key)
        for k, it in self._node_items.items():
            it.setOpacity(1.0 if k in neighbors else FADE_OPACITY)
        for edge in self._edges:
            if edge in hl_edges:
                edge.set_state(EdgeItem.STATE_HILITE)
            else:
                edge.set_state(EdgeItem.STATE_FADED)

    def _on_node_unhover(self, item):
        for it in self._node_items.values():
            it.setOpacity(1.0)
        for edge in self._edges:
            edge.set_state(EdgeItem.STATE_DEFAULT)

    # --- zoom / pan ---------------------------------------------------------
    def _zoom_bounds(self):
        """Bornes de zoom, adaptées à la taille du graphe.

        Un graphe très large impose un facteur d'ajustement minuscule : la
        borne basse doit descendre au moins jusque-là, sinon on ne peut plus
        zoomer du tout.
        """
        low, high = ZOOM_MIN, ZOOM_MAX
        rect = self._visible_bounding_rect()
        if not rect.isEmpty():
            vp = self.viewport().rect()
            if rect.width() > 0 and rect.height() > 0:
                fit = min(vp.width() / rect.width(),
                          vp.height() / rect.height())
                low = min(low, fit * 0.5)
        return low, high

    def wheelEvent(self, event):
        factor = ZOOM_STEP if event.angleDelta().y() > 0 else 1.0 / ZOOM_STEP
        # La transformation fait foi (évite toute dérive de _zoom).
        current = self.transform().m11()
        low, high = self._zoom_bounds()
        # On borne le RÉSULTAT au lieu de refuser le geste : un zoom qui
        # ramène vers la plage autorisée reste toujours possible.
        target = max(low, min(high, current * factor))
        if current <= 0 or abs(target - current) < 1e-12:
            event.accept()
            return
        self.scale(target / current, target / current)
        self._zoom = target
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            # Pan au bouton du milieu (le bouton gauche déplace les nœuds).
            self._panning = True
            self._pan_last = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        # Clic gauche dans le vide : désélectionne le lien courant.
        if event.button() == Qt.LeftButton:
            hit = self.itemAt(event.position().toPoint())
            if not isinstance(hit, EdgeItem):
                self.clear_edge_selection()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning and self._pan_last is not None:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self._pan_last = None
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # --- actions ------------------------------------------------------------
    def reset_view(self):
        """Recentre et ajuste le zoom pour voir tout le graphe."""
        rect = self._visible_bounding_rect()
        if rect.isEmpty():
            return
        self.resetTransform()
        self._zoom = 1.0
        self.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.KeepAspectRatio)
        # Mémorise le facteur d'échelle réel après fitInView.
        self._zoom = self.transform().m11()

    def reset_layout(self):
        """Rétablit l'ordre des lignes et les colonnes d'origine."""
        if not self._rows:
            return
        self._rows = list(self._initial_rows)
        for key, item in self._node_items.items():
            pos = self._initial_pos.get(key)
            if pos is not None:
                item.setPos(QPointF(pos.x(), item.y()))   # restaure le X
        # On rétablit vraiment la disposition d'origine : resserrer le graphe
        # est le rôle de « Redraw layout ».
        self._reflow()
        self.reset_view()
