
import os
import sys
import time
import socket
from collections import deque

import psutil
from PyQt5 import QtCore, QtGui, QtWidgets


APP_TITLE = "SysViewMac"
REFRESH_MS = 1000
HISTORY_POINTS = 120


def fmt_bytes(value: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(value)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def fmt_rate(value: float) -> str:
    return f"{fmt_bytes(value)}/s"


def safe_delta(new_value: int, old_value: int) -> int:
    return max(0, int(new_value) - int(old_value))


class SparklineWidget(QtWidgets.QWidget):
    def __init__(self, title: str = "", percent_mode: bool = False, parent=None):
        super().__init__(parent)
        self.title = title
        self.percent_mode = percent_mode
        self.series = []
        self.setMinimumHeight(160)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def set_series(self, series):
        """
        series: list of dicts:
            {"name": "CPU", "values": [...], "color": QColor(...)}
        """
        self.series = series
        self.update()

    def _max_value(self) -> float:
        if self.percent_mode:
            return 100.0

        max_v = 1.0
        for item in self.series:
            values = item.get("values", [])
            if values:
                max_v = max(max_v, max(values))
        return max_v * 1.15

    def paintEvent(self, event):
        del event
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        outer = self.rect()
        p.fillRect(outer, QtGui.QColor("#161b22"))

        rect = outer.adjusted(12, 12, -12, -12)
        p.setPen(QtGui.QPen(QtGui.QColor("#26303c"), 1))
        p.drawRoundedRect(rect, 10, 10)

        chart = rect.adjusted(10, 28, -10, -12)

        # Grid
        p.setPen(QtGui.QPen(QtGui.QColor("#233142"), 1, QtCore.Qt.DotLine))
        grid_lines = 4
        for i in range(grid_lines + 1):
            y = chart.top() + (chart.height() * i / grid_lines)
            p.drawLine(chart.left(), int(y), chart.right(), int(y))

        # Title
        p.setPen(QtGui.QColor("#d0d7de"))
        title_font = p.font()
        title_font.setBold(True)
        p.setFont(title_font)
        p.drawText(rect.adjusted(8, 0, -8, 0), QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft, self.title)

        # Scale label
        max_v = self._max_value()
        p.setPen(QtGui.QColor("#8b949e"))
        info_font = p.font()
        info_font.setBold(False)
        info_font.setPointSize(max(8, info_font.pointSize() - 1))
        p.setFont(info_font)
        if self.percent_mode:
            scale_text = "0–100 %"
        else:
            scale_text = f"max {fmt_rate(max_v)}"
        p.drawText(rect.adjusted(8, 0, -8, 0), QtCore.Qt.AlignTop | QtCore.Qt.AlignRight, scale_text)

        if chart.width() <= 10 or chart.height() <= 10:
            return

        for item in self.series:
            values = item.get("values", [])
            color = item.get("color", QtGui.QColor("#58a6ff"))
            if len(values) < 2:
                continue

            path = QtGui.QPainterPath()
            step_x = chart.width() / max(1, len(values) - 1)

            def map_y(v):
                if max_v <= 0.0:
                    return chart.bottom()
                ratio = max(0.0, min(float(v) / max_v, 1.0))
                return chart.bottom() - ratio * chart.height()

            path.moveTo(chart.left(), map_y(values[0]))
            for idx, value in enumerate(values[1:], start=1):
                x = chart.left() + idx * step_x
                y = map_y(value)
                path.lineTo(x, y)

            pen = QtGui.QPen(color, 2)
            p.setPen(pen)
            p.drawPath(path)

        # Legend
        x = rect.left() + 10
        y = rect.bottom() - 18
        for item in self.series:
            name = item.get("name", "")
            color = item.get("color", QtGui.QColor("#58a6ff"))
            values = item.get("values", [])
            if not values:
                continue
            current = values[-1]
            if self.percent_mode:
                text = f"{name}: {current:.1f}%"
            else:
                text = f"{name}: {fmt_rate(current)}"
            p.setPen(QtGui.QPen(color, 6))
            p.drawLine(x, y, x + 12, y)
            p.setPen(QtGui.QColor("#c9d1d9"))
            p.drawText(x + 18, y + 4, text)
            x += max(120, p.fontMetrics().horizontalAdvance(text) + 36)


class CoreBar(QtWidgets.QWidget):
    def __init__(self, core_index: int, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.label = QtWidgets.QLabel(f"C{core_index:02d}")
        self.label.setFixedWidth(42)
        self.label.setStyleSheet("color: #8b949e;")

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        self.bar.setFormat("%p%")
        self.bar.setMinimumHeight(18)

        layout.addWidget(self.label)
        layout.addWidget(self.bar, 1)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1320, 900)

        self.cpu_hist = deque(maxlen=HISTORY_POINTS)
        self.mem_hist = deque(maxlen=HISTORY_POINTS)
        self.disk_read_hist = deque(maxlen=HISTORY_POINTS)
        self.disk_write_hist = deque(maxlen=HISTORY_POINTS)
        self.net_recv_hist = deque(maxlen=HISTORY_POINTS)
        self.net_sent_hist = deque(maxlen=HISTORY_POINTS)

        self.prev_ts = time.monotonic()
        self.prev_disk = psutil.disk_io_counters()
        self.prev_net = psutil.net_io_counters()

        # Prime cpu counters; first non-blocking call is meaningless.
        psutil.cpu_percent(interval=None, percpu=True)

        self._build_ui()
        self._apply_style()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.sample)
        self.timer.start(REFRESH_MS)

        self.sample()

    def _build_ui(self):
        cw = QtWidgets.QWidget()
        self.setCentralWidget(cw)

        root = QtWidgets.QVBoxLayout(cw)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        header = QtWidgets.QFrame()
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(12, 10, 12, 10)

        host = socket.gethostname()
        logical = psutil.cpu_count(logical=True) or 1
        physical = psutil.cpu_count(logical=False) or logical

        self.header_title = QtWidgets.QLabel(f"{APP_TITLE}  |  {host}")
        self.header_title.setObjectName("HeaderTitle")

        self.header_info = QtWidgets.QLabel(
            f"refresh={REFRESH_MS} ms   logical={logical}   physical={physical}"
        )
        self.header_info.setStyleSheet("color: #8b949e;")

        header_layout.addWidget(self.header_title)
        header_layout.addStretch(1)
        header_layout.addWidget(self.header_info)

        root.addWidget(header)

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        root.addLayout(grid, 1)

        self.cpu_box = self._make_group("CPU")
        self.mem_box = self._make_group("Memory")
        self.disk_box = self._make_group("Disk I/O")
        self.net_box = self._make_group("Network")

        grid.addWidget(self.cpu_box, 0, 0)
        grid.addWidget(self.mem_box, 0, 1)
        grid.addWidget(self.disk_box, 1, 0)
        grid.addWidget(self.net_box, 1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self._build_cpu_box()
        self._build_mem_box()
        self._build_disk_box()
        self._build_net_box()

    def _make_group(self, title: str) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(title)
        box.setLayout(QtWidgets.QVBoxLayout())
        box.layout().setContentsMargins(12, 18, 12, 12)
        box.layout().setSpacing(10)
        return box

    def _make_kv_label_pair(self, layout, key_text):
        key = QtWidgets.QLabel(key_text)
        key.setStyleSheet("color: #8b949e;")
        value = QtWidgets.QLabel("-")
        value.setStyleSheet("color: #e6edf3; font-weight: 600;")

        if isinstance(layout, QtWidgets.QGridLayout):
            row = layout.rowCount()
            layout.addWidget(key, row, 0)
            layout.addWidget(value, row, 1, QtCore.Qt.AlignRight)
        else:
            row = QtWidgets.QHBoxLayout()
            row.addWidget(key)
            row.addStretch(1)
            row.addWidget(value)
            layout.addLayout(row)
        return value

    def _build_cpu_box(self):
        top = QtWidgets.QGridLayout()
        top.setHorizontalSpacing(18)
        top.setVerticalSpacing(6)
        self.cpu_box.layout().addLayout(top)

        self.cpu_total_label = self._make_kv_label_pair(top, "Total")
        self.cpu_load_label = self._make_kv_label_pair(top, "Load avg")
        self.cpu_cores_label = self._make_kv_label_pair(top, "Cores")

        self.cpu_chart = SparklineWidget("CPU history", percent_mode=True)
        self.cpu_box.layout().addWidget(self.cpu_chart)

        self.core_area = QtWidgets.QScrollArea()
        self.core_area.setWidgetResizable(True)
        self.core_area.setFrameShape(QtWidgets.QFrame.NoFrame)

        core_container = QtWidgets.QWidget()
        self.core_grid = QtWidgets.QGridLayout(core_container)
        self.core_grid.setContentsMargins(0, 0, 0, 0)
        self.core_grid.setHorizontalSpacing(12)
        self.core_grid.setVerticalSpacing(6)
        self.core_area.setWidget(core_container)
        self.cpu_box.layout().addWidget(self.core_area, 1)

        self.core_widgets = []
        core_count = psutil.cpu_count(logical=True) or 1
        columns = 2 if core_count <= 8 else 3 if core_count <= 18 else 4

        for idx in range(core_count):
            widget = CoreBar(idx)
            row = idx // columns
            col = idx % columns
            self.core_grid.addWidget(widget, row, col)
            self.core_widgets.append(widget)

    def _build_mem_box(self):
        top = QtWidgets.QGridLayout()
        top.setHorizontalSpacing(18)
        top.setVerticalSpacing(6)
        self.mem_box.layout().addLayout(top)

        self.mem_used_label = self._make_kv_label_pair(top, "Used")
        self.mem_avail_label = self._make_kv_label_pair(top, "Available")
        self.mem_swap_label = self._make_kv_label_pair(top, "Swap")

        self.mem_bar = QtWidgets.QProgressBar()
        self.mem_bar.setRange(0, 100)
        self.mem_bar.setValue(0)
        self.mem_bar.setFormat("%p% used")
        self.mem_bar.setMinimumHeight(24)
        self.mem_box.layout().addWidget(self.mem_bar)

        self.mem_chart = SparklineWidget("Memory history", percent_mode=True)
        self.mem_box.layout().addWidget(self.mem_chart, 1)

    def _build_disk_box(self):
        top = QtWidgets.QGridLayout()
        top.setHorizontalSpacing(18)
        top.setVerticalSpacing(6)
        self.disk_box.layout().addLayout(top)

        self.disk_read_label = self._make_kv_label_pair(top, "Read")
        self.disk_write_label = self._make_kv_label_pair(top, "Write")
        self.disk_total_label = self._make_kv_label_pair(top, "Total")

        self.disk_chart = SparklineWidget("Disk throughput")
        self.disk_box.layout().addWidget(self.disk_chart, 1)

    def _build_net_box(self):
        top = QtWidgets.QGridLayout()
        top.setHorizontalSpacing(18)
        top.setVerticalSpacing(6)
        self.net_box.layout().addLayout(top)

        self.net_recv_label = self._make_kv_label_pair(top, "Receive")
        self.net_sent_label = self._make_kv_label_pair(top, "Send")
        self.net_total_label = self._make_kv_label_pair(top, "Total")

        self.net_chart = SparklineWidget("Network throughput")
        self.net_box.layout().addWidget(self.net_chart, 1)

    def _apply_style(self):
        QtWidgets.QApplication.setStyle("Fusion")
        self.setStyleSheet(
            """
            QWidget {
                background: #0d1117;
                color: #c9d1d9;
                font-size: 13px;
            }
            QMainWindow {
                background: #0d1117;
            }
            QGroupBox {
                border: 1px solid #26303c;
                border-radius: 10px;
                margin-top: 10px;
                font-weight: 600;
                padding-top: 8px;
                background: #11161d;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
                color: #e6edf3;
            }
            QFrame {
                border: 1px solid #26303c;
                border-radius: 10px;
                background: #11161d;
            }
            QLabel#HeaderTitle {
                font-size: 18px;
                font-weight: 700;
                color: #f0f6fc;
            }
            QProgressBar {
                border: 1px solid #26303c;
                border-radius: 8px;
                background: #0d1117;
                text-align: center;
                color: #f0f6fc;
                min-height: 18px;
            }
            QProgressBar::chunk {
                background-color: #238636;
                border-radius: 7px;
            }
            QScrollArea {
                border: none;
                background: transparent;
            }
            """
        )

    def sample(self):
        now = time.monotonic()
        dt = max(now - self.prev_ts, 1e-6)

        per_core = psutil.cpu_percent(interval=None, percpu=True)
        cpu_total = sum(per_core) / len(per_core) if per_core else 0.0

        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()

        disk = psutil.disk_io_counters()
        net = psutil.net_io_counters()

        if disk is not None and self.prev_disk is not None:
            read_rate = safe_delta(disk.read_bytes, self.prev_disk.read_bytes) / dt
            write_rate = safe_delta(disk.write_bytes, self.prev_disk.write_bytes) / dt
            total_disk = disk.read_bytes + disk.write_bytes
        else:
            read_rate = 0.0
            write_rate = 0.0
            total_disk = 0

        if net is not None and self.prev_net is not None:
            recv_rate = safe_delta(net.bytes_recv, self.prev_net.bytes_recv) / dt
            sent_rate = safe_delta(net.bytes_sent, self.prev_net.bytes_sent) / dt
            total_net = net.bytes_recv + net.bytes_sent
        else:
            recv_rate = 0.0
            sent_rate = 0.0
            total_net = 0

        self.prev_ts = now
        self.prev_disk = disk
        self.prev_net = net

        self.cpu_hist.append(cpu_total)
        self.mem_hist.append(vm.percent)
        self.disk_read_hist.append(read_rate)
        self.disk_write_hist.append(write_rate)
        self.net_recv_hist.append(recv_rate)
        self.net_sent_hist.append(sent_rate)

        self.cpu_total_label.setText(f"{cpu_total:.1f}%")
        self.cpu_cores_label.setText(str(len(per_core)))
        try:
            load1, load5, load15 = os.getloadavg()
            self.cpu_load_label.setText(f"{load1:.2f} / {load5:.2f} / {load15:.2f}")
        except (AttributeError, OSError):
            self.cpu_load_label.setText("n/a")

        for idx, value in enumerate(per_core):
            if idx < len(self.core_widgets):
                self.core_widgets[idx].bar.setValue(int(round(value)))
                self.core_widgets[idx].bar.setFormat(f"{value:.1f}%")

        self.mem_used_label.setText(f"{fmt_bytes(vm.used)} / {fmt_bytes(vm.total)}")
        self.mem_avail_label.setText(fmt_bytes(vm.available))
        self.mem_swap_label.setText(f"{fmt_bytes(sm.used)} / {fmt_bytes(sm.total)}")
        self.mem_bar.setValue(int(round(vm.percent)))
        self.mem_bar.setFormat(f"{vm.percent:.1f}% used")

        self.disk_read_label.setText(fmt_rate(read_rate))
        self.disk_write_label.setText(fmt_rate(write_rate))
        self.disk_total_label.setText(fmt_bytes(total_disk))

        self.net_recv_label.setText(fmt_rate(recv_rate))
        self.net_sent_label.setText(fmt_rate(sent_rate))
        self.net_total_label.setText(fmt_bytes(total_net))

        self.cpu_chart.set_series([
            {"name": "CPU", "values": list(self.cpu_hist), "color": QtGui.QColor("#58a6ff")}
        ])
        self.mem_chart.set_series([
            {"name": "RAM", "values": list(self.mem_hist), "color": QtGui.QColor("#3fb950")}
        ])
        self.disk_chart.set_series([
            {"name": "Read", "values": list(self.disk_read_hist), "color": QtGui.QColor("#58a6ff")},
            {"name": "Write", "values": list(self.disk_write_hist), "color": QtGui.QColor("#f778ba")},
        ])
        self.net_chart.set_series([
            {"name": "RX", "values": list(self.net_recv_hist), "color": QtGui.QColor("#79c0ff")},
            {"name": "TX", "values": list(self.net_sent_hist), "color": QtGui.QColor("#ffa657")},
        ])


def main():
    if hasattr(QtCore.Qt, "AA_EnableHighDpiScaling"):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    if hasattr(QtCore.Qt, "AA_UseHighDpiPixmaps"):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
