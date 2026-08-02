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

# --- Palette (dark, sobre) --------------------------------------------------
COL_BG = QColor("#14171b")
COL_TITLE = QColor("#eef1f5")
COL_SUB = QColor("#b4bcc6")
COL_META_START = QColor("#f0c96a")
COL_META_WARN = QColor("#eaa24d")
COL_ROW_LABEL = QColor("#7b8593")
COL_ROW_GUIDE = QColor(255, 255, 255, 12)

COL_OK_FILL = QColor("#22402f")
COL_OK_BORDER = QColor("#4fa06d")
COL_STALE_FILL = QColor("#46282b")
COL_STALE_BORDER = QColor("#c9564e")
COL_START_BORDER = QColor("#e6b84c")

COL_EDGE_DEFAULT = QColor(172, 180, 192, 90)
COL_EDGE_FADED = QColor(150, 158, 170, 28)
COL_EDGE_HILITE = QColor(233, 237, 245, 230)

# --- Géométrie --------------------------------------------------------------
NODE_W = 200.0
PAD = 11.0
COL_W = NODE_W + 52.0
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
        self._meta_lines = []     # list[(texte, couleur)]
        self._sub_lines = []

        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(0)
        self.setToolTip(self._tooltip_text())
        self._relayout()

    # --- géométrie / contenu ------------------------------------------------
    def _tooltip_text(self):
        n = self.node
        parts = [n.display_name]
        if n.node_names:
            parts.append("nodes : " + ", ".join(n.node_names))
        if not n.is_latest and n.latest_version is not None:
            parts.append(f"dernière version : v{n.latest_version:03d}")
        parts.append("à jour" if n.status == "ok" else "périmé")
        return "\n".join(parts)

    def _relayout(self):
        self.prepareGeometryChange()
        n = self.node
        inner_w = NODE_W - 2 * PAD

        fm_title = QFontMetricsF(self._title_font)
        fm_sub = QFontMetricsF(self._sub_font)
        fm_meta = QFontMetricsF(self._meta_font)

        # Titre (nom canonique) sur 1-2 lignes, avec élision si nécessaire.
        title_lines = _wrap(n.display_name.split("_"), "_", fm_title, inner_w)
        if len(title_lines) > 2:
            title_lines = [title_lines[0],
                           "_".join(title_lines[1:])]
        self._title_lines = [
            fm_title.elidedText(t, Qt.ElideRight, inner_w) for t in title_lines
        ]

        # Lignes méta (départ / version périmée).
        self._meta_lines = []
        if n.is_start:
            self._meta_lines.append(("— scène de départ —", COL_META_START))
        if not n.is_latest and n.latest_version is not None:
            self._meta_lines.append(
                (f"⚠ dernière : v{n.latest_version:03d}", COL_META_WARN))

        # Liste des nodes abrégés, séparés par « · ».
        if n.node_names:
            words = [abbreviate_node(name) for name in n.node_names]
            self._sub_lines = _wrap(words, " · ", fm_sub, inner_w)
            self._sub_lines = [
                fm_sub.elidedText(t, Qt.ElideRight, inner_w)
                for t in self._sub_lines
            ]
        else:
            self._sub_lines = []

        # Hauteur totale.
        h = PAD
        h += len(self._title_lines) * (fm_title.height())
        if self._meta_lines:
            h += 3 + len(self._meta_lines) * fm_meta.height()
        if self._sub_lines:
            h += 6 + len(self._sub_lines) * fm_sub.height()
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
        painter.setPen(QPen(COL_TITLE))
        for line in self._title_lines:
            y += fm_title.ascent()
            painter.drawText(QPointF(x, y), line)
            y += fm_title.descent() + fm_title.leading()

        if self._meta_lines:
            y += 3
            fm_meta = QFontMetricsF(self._meta_font)
            painter.setFont(self._meta_font)
            for text, color in self._meta_lines:
                y += fm_meta.ascent()
                painter.setPen(QPen(color))
                painter.drawText(QPointF(x, y), text)
                y += fm_meta.descent() + fm_meta.leading()

        if self._sub_lines:
            y += 6
            fm_sub = QFontMetricsF(self._sub_font)
            painter.setFont(self._sub_font)
            painter.setPen(QPen(COL_SUB))
            for line in self._sub_lines:
                y += fm_sub.ascent()
                painter.drawText(QPointF(x, y), line)
                y += fm_sub.descent() + fm_sub.leading()


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
        self._initial_pos = {}      # key -> QPointF (pour reset layout)
        self._zoom = 1.0

        self._panning = False
        self._pan_last = None

    # --- construction -------------------------------------------------------
    def clear_graph(self):
        self._scene.clear()
        self._node_items.clear()
        self._edges.clear()
        self._initial_pos.clear()

    def set_graph(self, result):
        """Affiche un graph_model.GraphResult."""
        self.clear_graph()
        if not result.nodes:
            return

        # 1) créer les items (hauteurs connues ensuite).
        for key, node in result.nodes.items():
            item = SceneNodeItem(node)
            item.hovered.connect(self._on_node_hover)
            item.unhovered.connect(self._on_node_unhover)
            self._node_items[key] = item
            self._scene.addItem(item)

        # 2) hauteur de chaque ligne = plus haut nœud de la ligne.
        rows = {}
        for key, item in self._node_items.items():
            rows.setdefault(item.node.row, []).append(item)
        max_row = max(rows)
        row_h = {r: max((it.height() for it in rows[r]), default=MIN_ROW_H)
                 for r in rows}
        row_y = {}
        y = MARGIN_TOP
        for r in range(max_row + 1):
            row_y[r] = y
            y += row_h.get(r, MIN_ROW_H) + ROW_GAP

        # 3) positionner les nœuds (X = colonne, Y = ligne verrouillée).
        for key, item in self._node_items.items():
            node = item.node
            x = MARGIN_LEFT + node.col * COL_W
            yy = row_y[node.row]
            item.set_row_y(yy)
            item.setPos(QPointF(x, yy))
            self._initial_pos[key] = QPointF(x, yy)

        # 4) libellés de ligne + guides horizontaux.
        scene_right = MARGIN_LEFT + (max(
            (it.node.col for it in self._node_items.values()), default=0
        ) + 1) * COL_W
        for r, task in result.row_tasks.items():
            yy = row_y[r]
            guide = self._scene.addLine(
                QLineF(MARGIN_LEFT - 18, yy - ROW_GAP * 0.4,
                       scene_right, yy - ROW_GAP * 0.4),
                QPen(COL_ROW_GUIDE, 1.0))
            guide.setZValue(-3)
            label = QGraphicsSimpleTextItem(task)
            f = QFont()
            f.setBold(True)
            f.setPointSizeF(9.5)
            label.setFont(f)
            label.setBrush(QBrush(COL_ROW_LABEL))
            label.setPos(QPointF(14, yy + 2))
            label.setZValue(-2)
            self._scene.addItem(label)

        # 5) arêtes.
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

        margin = 80.0
        self._scene.setSceneRect(
            self._scene.itemsBoundingRect().adjusted(
                -margin, -margin, margin, margin))
        self.reset_view()

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
        """Remet chaque nœud à sa position initiale (colonnes calculées)."""
        for key, item in self._node_items.items():
            pos = self._initial_pos.get(key)
            if pos is not None:
                item.set_row_y(pos.y())
                item.setPos(pos)
        for edge in self._edges:
            edge.update_path()
        self.reset_view()
