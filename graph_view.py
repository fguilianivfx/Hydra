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

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
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
COL_INHERITED_FILL = QColor("#f6ddb2") # pêche pastel
COL_INHERITED_BORDER = QColor("#e3bd80")
COL_START_BORDER = QColor("#d9a93f")   # or, accent de la scène interrogée

# Couleur du texte d'un output/input selon sa fraîcheur (foncé sur pastel).
COL_ASSET_OK = QColor("#1c7a44")       # vert foncé
COL_ASSET_STALE = QColor("#b4392c")    # rouge foncé

COL_EDGE_DEFAULT = QColor(172, 180, 192, 90)
COL_EDGE_FADED = QColor(150, 158, 170, 28)
COL_EDGE_HILITE = QColor(233, 237, 245, 230)

# Poignée de ligne (à gauche) + séparateur asset/shot.
COL_HEADER_BG = QColor("#20242b")
COL_HEADER_HOVER = QColor("#2c323b")
COL_HEADER_BORDER = QColor("#3a414c")
COL_SEPARATOR = QColor("#727e8f")

# --- Géométrie --------------------------------------------------------------
NODE_W = 200.0
PAD = 11.0
COL_W = NODE_W + 52.0
HEADER_H = 26.0            # hauteur de la poignée de ligne
SEP_EXTRA = 34.0          # espace supplémentaire autour du séparateur
GUIDE_DX = 18.0
ROW_GAP = 74.0
MARGIN_LEFT = 156.0
MARGIN_TOP = 46.0
MIN_ROW_H = 70.0

FADE_OPACITY = 0.16


def abbreviate_node(name):
    """Abrège un node_name long (camera_layer_01_camera_abc -> camera)."""
    if not name:
        return ""
    if len(name) <= 16:
        return name
    head = name.split("_")[0]
    if head and len(head) <= 16:
        return head
    return name[:14] + "…"


def _vfmt(version):
    """Formate une version : 19 -> 'v019' ; None -> 'v?'."""
    return f"v{version:03d}" if version is not None else "v?"


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

    def __init__(self, src_item, dst_item, top_key, bottom_key):
        super().__init__()
        self.src = src_item          # parent (row du haut)
        self.dst = dst_item          # enfant (row du bas)
        self.top_key = top_key
        self.bottom_key = bottom_key
        self._arrow_size = 9.0
        self._state = self.STATE_DEFAULT
        # Cintrage horizontal des arêtes qui sautent des lignes, pour rester
        # visibles quand les nœuds sont empilés dans la même colonne.
        self._row_span = abs(dst_item.node.row - src_item.node.row)
        self._bow_dir = 1.0 if (hash((top_key, bottom_key)) % 2 == 0) else -1.0
        self.setZValue(-1)
        self.setAcceptHoverEvents(False)
        self._apply_pen()
        self.update_path()

    def _apply_pen(self):
        if self._state == self.STATE_HILITE:
            pen = QPen(COL_EDGE_HILITE, 2.2)
        elif self._state == self.STATE_FADED:
            pen = QPen(COL_EDGE_FADED, 1.1)
        else:
            pen = QPen(COL_EDGE_DEFAULT, 1.3)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        self._pen = pen
        self.setPen(pen)
        self.update()

    def set_state(self, state):
        if state != self._state:
            self._state = state
            self.setZValue(1 if state == self.STATE_HILITE else -1)
            self._apply_pen()

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
        extra = self._arrow_size + self._pen.widthF() + 2.0
        return super().boundingRect().adjusted(-extra, -extra, extra, extra)

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
    @staticmethod
    def _version_state(current, latest):
        """« (vXXX) » si à jour, « (vXXX → vYYY) » si périmé."""
        if (current is not None and latest is not None and current < latest):
            return f"({_vfmt(current)} → {_vfmt(latest)})"
        return f"({_vfmt(current)})"

    def _tooltip_text(self):
        n = self.node
        parts = [n.display_name]
        parts.append("Artist: "
                     + (", ".join(n.artists) if n.artists else "unknown"))
        if n.inputs:
            parts.append("Inputs:")
            for label, cur, latest in n.inputs:
                parts.append(f"  {label} {self._version_state(cur, latest)}")
        if n.outputs:
            parts.append("Outputs:")
            for name, latest in n.outputs:
                parts.append(
                    f"  {name} {self._version_state(n.version, latest)}")
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
        for name, latest in n.outputs:
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

    def set_graph(self, result):
        """Affiche un graph_model.GraphResult."""
        self.clear_graph()
        if not result.nodes:
            return

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
            edge = EdgeItem(src, dst, top_key, bottom_key)
            self._scene.addItem(edge)
            self._edges.append(edge)
            src.add_edge(edge)
            dst.add_edge(edge)

        # 5) placement.
        self._reflow(record_initial=True)
        self.reset_view()

    # --- disposition des lignes --------------------------------------------
    def _scene_right(self):
        max_col = max((it.node.col for it in self._node_items.values()),
                      default=0)
        return MARGIN_LEFT + (max_col + 1) * COL_W

    def _separator_after_index(self):
        """Indice de la dernière ligne « asset » suivie d'au moins une autre."""
        asset_idx = [i for i, row in enumerate(self._rows)
                     if row.level == "asset"]
        if not asset_idx:
            return None
        last = max(asset_idx)
        return last if last < len(self._rows) - 1 else None

    def _reflow(self, record_initial=False):
        """Recalcule les Y de chaque ligne et repositionne tout."""
        if not self._rows:
            return
        self._reflowing = True
        right = self._scene_right()
        sep_after = self._separator_after_index()
        y = MARGIN_TOP
        for i, row in enumerate(self._rows):
            row.top = y
            height = max((it.height() for it in row.nodes), default=MIN_ROW_H)
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

    def _update_scene_rect(self):
        margin = 80.0
        self._scene.setSceneRect(
            self._scene.itemsBoundingRect().adjusted(
                -margin, -margin, margin, margin))

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
    def wheelEvent(self, event):
        step = 1.15
        if event.angleDelta().y() > 0:
            factor = step
        else:
            factor = 1.0 / step
        new_zoom = self._zoom * factor
        if new_zoom < 0.08 or new_zoom > 8.0:
            return
        self._zoom = new_zoom
        self.scale(factor, factor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            # Pan au bouton du milieu (le bouton gauche déplace les nœuds).
            self._panning = True
            self._pan_last = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
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
        rect = self._scene.itemsBoundingRect()
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
        self._reflow()
        self.reset_view()
