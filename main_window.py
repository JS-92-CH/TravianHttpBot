import sys
import copy
import threading
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QLineEdit,
    QCheckBox, QFormLayout, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QTextEdit, QSplitter, QGroupBox, QComboBox, QSpinBox,
    QMessageBox, QScrollArea, QFrame
)
from PyQt6.QtCore import Qt, pyqtSlot, QThread, QObject, pyqtSignal
from PyQt6.QtGui import QColor

from bot import BotManager
from config import BOT_STATE, state_lock, save_config, log, setup_logging, GID_MAPPING
from qt_handler import QtLogHandler
from proxy_util import get_fastest_proxies, parse_proxy_file

# --- Worker thread for non-GUI tasks like fetching proxies ---
class Worker(QObject):
    finished = pyqtSignal(list)

    @pyqtSlot()
    def run(self):
        proxies = get_fastest_proxies()
        self.finished.emit(proxies)

# --- Draggable List Widget for Priority Queues ---
class DraggableListWidget(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setAcceptDrops(True)

# --- Main Window Class ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Travian Bot Dashboard")
        self.setGeometry(100, 100, 1800, 1000)

        self.FULL_STATE = {}
        self.selected_account_username = None
        self.selected_village_id = None
        self.global_active_tab_index = 0

        self._setup_ui()
        self._setup_bot_manager()
        self._apply_stylesheet()

        with state_lock:
            self.FULL_STATE = copy.deepcopy(BOT_STATE)
        self.render_all_ui()

    def _setup_ui(self):
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        log_splitter = QSplitter(Qt.Orientation.Vertical)

        self.left_column = self._create_accounts_panel()
        self.middle_column = self._create_villages_panel()
        self.right_column = self._create_details_panel()
        self.log_panel = self._create_log_panel()

        main_splitter.addWidget(self.left_column)
        main_splitter.addWidget(self.middle_column)
        main_splitter.addWidget(self.right_column)
        main_splitter.setStretchFactor(0, 2); main_splitter.setStretchFactor(1, 2); main_splitter.setStretchFactor(2, 5)

        log_splitter.addWidget(main_splitter)
        log_splitter.addWidget(self.log_panel)
        log_splitter.setStretchFactor(0, 4); log_splitter.setStretchFactor(1, 1)

        self.setCentralWidget(log_splitter)

    def move_queue_item(self, direction):
        if not self.selected_village_id or self.queue_list_widget.currentRow() < 0:
            return

        vid_str = str(self.selected_village_id)
        current_index = self.queue_list_widget.currentRow()

        with state_lock:
            queue = BOT_STATE.get('build_queues', {}).get(vid_str, [])
            if not (0 <= current_index < len(queue)):
                return

            item = queue.pop(current_index)

            if direction == 'top':
                queue.insert(0, item)
            elif direction == 'up':
                queue.insert(max(0, current_index - 1), item)
            elif direction == 'down':
                queue.insert(min(len(queue), current_index + 1), item)
            elif direction == 'bottom':
                queue.append(item)
            
        save_config()
        log.info(f"Build queue reordered for village {vid_str}")
        self.repopulate_queue_list() # Refresh the list to show changes

    def remove_queue_item(self):
        if not self.selected_village_id or self.queue_list_widget.currentRow() < 0:
            return

        vid_str = str(self.selected_village_id)
        current_index = self.queue_list_widget.currentRow()

        with state_lock:
            queue = BOT_STATE.get('build_queues', {}).get(vid_str, [])
            if (0 <= current_index < len(queue)):
                removed_item = queue.pop(current_index)
                log.info(f"Removed item from queue for village {vid_str}: {removed_item}")
        
        save_config()
        self.repopulate_queue_list() # Refresh the list

    def _create_accounts_panel(self):
        widget = QWidget(); layout = QVBoxLayout(widget)
        title = QLabel("Accounts"); title.setObjectName("title")
        layout.addWidget(title)
        self.account_list_widget = QListWidget(); self.account_list_widget.itemClicked.connect(self.select_account)
        layout.addWidget(self.account_list_widget)
        add_account_group = QGroupBox("Add New Account"); add_account_group.setCheckable(True); add_account_group.setChecked(False)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll_content = QWidget(); form_layout = QFormLayout(scroll_content)
        self.username_input = QLineEdit()
        self.password_input = QLineEdit(); self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.server_url_input = QLineEdit()
        self.is_sitter_checkbox = QCheckBox("Is this a sitter account?")
        self.sitter_for_input = QLineEdit()
        self.use_dual_queue_checkbox = QCheckBox("Use Plus Account (2 builds)")
        self.use_hero_resources_checkbox = QCheckBox("Use Hero Resources")
        self.proxy_ip_input = QLineEdit(); self.proxy_port_input = QLineEdit()
        self.proxy_user_input = QLineEdit(); self.proxy_pass_input = QLineEdit(); self.proxy_pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_dropdown = QComboBox()
        self.fetch_proxies_btn = QPushButton("Fetch Fastest Proxies")
        self.fetch_proxies_btn.clicked.connect(self.fetch_proxies_in_thread)
        self.proxy_dropdown.currentIndexChanged.connect(self.select_proxy_from_dropdown)
        add_account_button = QPushButton("Add Account"); add_account_button.clicked.connect(self.add_account)
        form_layout.addRow("Username:", self.username_input)
        form_layout.addRow("Password:", self.password_input)
        form_layout.addRow("Server URL:", self.server_url_input)
        form_layout.addRow(self.is_sitter_checkbox); form_layout.addRow("Sitting for:", self.sitter_for_input)
        form_layout.addRow(self.use_dual_queue_checkbox); form_layout.addRow(self.use_hero_resources_checkbox)
        proxy_separator = QFrame(); proxy_separator.setFrameShape(QFrame.Shape.HLine)
        form_layout.addRow(proxy_separator); form_layout.addRow(QLabel("<b>Proxy (Optional)</b>"))
        form_layout.addRow("Proxy IP:", self.proxy_ip_input); form_layout.addRow("Proxy Port:", self.proxy_port_input)
        form_layout.addRow("Proxy User:", self.proxy_user_input); form_layout.addRow("Proxy Pass:", self.proxy_pass_input)
        form_layout.addRow(self.fetch_proxies_btn); form_layout.addRow("Results:", self.proxy_dropdown)
        separator = QFrame(); separator.setFrameShape(QFrame.Shape.HLine)
        form_layout.addRow(separator); form_layout.addRow(add_account_button)
        scroll.setWidget(scroll_content)
        group_layout = QVBoxLayout(); group_layout.addWidget(scroll)
        add_account_group.setLayout(group_layout)
        layout.addWidget(add_account_group)
        return widget

    def _create_villages_panel(self):
        widget = QWidget(); layout = QVBoxLayout(widget); title = QLabel("Villages"); title.setObjectName("title")
        layout.addWidget(title); self.village_list_widget = QListWidget(); self.village_list_widget.itemClicked.connect(self.select_village)
        layout.addWidget(self.village_list_widget); return widget

    def _create_details_panel(self):
        widget = QWidget(); self.details_layout = QVBoxLayout(widget); self.details_layout.setContentsMargins(0,0,0,0)
        self.details_stack = QWidget(); self.details_stack_layout = QVBoxLayout(self.details_stack)
        self.village_title_label = QLabel(); self.village_title_label.setObjectName("title")
        self.details_stack_layout.addWidget(self.village_title_label)
        self.tab_widget = QTabWidget(); self.tab_widget.currentChanged.connect(self.on_tab_changed)
        self.details_stack_layout.addWidget(self.tab_widget)
        self.placeholder_widget = QLabel("Select an account or village to see details."); self.placeholder_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder_widget.setStyleSheet("font-size: 16px; color: #6272a4;")
        self.details_layout.addWidget(self.placeholder_widget); self.details_layout.addWidget(self.details_stack)
        self.details_stack.hide(); return widget

    def _create_log_panel(self):
        widget = QGroupBox("Live Log"); layout = QVBoxLayout(widget); self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True); layout.addWidget(self.log_text_edit); return widget

    def fetch_proxies_in_thread(self):
        self.fetch_proxies_btn.setText("Fetching..."); self.fetch_proxies_btn.setEnabled(False)
        self.thread = QThread(); self.worker = Worker(); self.worker.moveToThread(self.thread)
        self.worker.finished.connect(self.on_proxy_fetch_finished); self.thread.started.connect(self.worker.run)
        self.thread.start()

    def on_proxy_fetch_finished(self, proxies):
        self.proxy_dropdown.clear(); self.proxy_dropdown.addItem("-- Select a Proxy --", None)
        for proxy in proxies: self.proxy_dropdown.addItem(f"{proxy['ip']}:{proxy['port']}", proxy)
        self.fetch_proxies_btn.setText("Fetch Fastest Proxies"); self.fetch_proxies_btn.setEnabled(True)
        self.thread.quit(); self.worker.deleteLater(); self.thread.deleteLater()

    def select_proxy_from_dropdown(self, index):
        proxy_data = self.proxy_dropdown.itemData(index)
        if proxy_data:
            self.proxy_ip_input.setText(proxy_data.get('ip', '')); self.proxy_port_input.setText(proxy_data.get('port', ''))
            self.proxy_user_input.setText(proxy_data.get('username', '')); self.proxy_pass_input.setText(proxy_data.get('password', ''))

    def _setup_bot_manager(self):
        self.log_handler = QtLogHandler(); self.log_handler.log_received.connect(self.update_log)
        setup_logging(self.log_handler); self.bot_manager = BotManager()
        self.bot_manager.gui_emitter.state_updated.connect(self.update_state)
        self.bot_manager.gui_emitter.villages_discovered.connect(self.handle_villages_discovered)
        self.bot_manager.start()

    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #1e1e2e;
                color: #f8f8f2;
                font-family: 'Segoe UI', sans-serif;
                font-size: 14px;
            }
            QMainWindow {
                background-color: #1e1e2e;
            }
            QGroupBox {
                background-color: #27293d;
                border: 1px solid #44475a;
                border-radius: 8px;
                margin-top: 10px;
                font-size: 14px;
                font-weight: bold;
                color: #bd93f9;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
                background-color: #27293d;
                border-radius: 4px;
            }
            QLabel#title {
                font-size: 18px;
                font-weight: bold;
                color: #50fa7b;
                padding-bottom: 10px;
                border-bottom: 1px solid #44475a;
                margin-bottom: 5px;
            }
            QListWidget {
                background-color: #1e1e2e;
                border: 1px solid #44475a;
                border-radius: 6px;
                padding: 5px;
            }
            QListWidget::item {
                background-color: #27293d;
                border: 1px solid #44475a;
                border-radius: 4px;
                margin-bottom: 3px;
            }
            QListWidget::item:selected {
                background-color: #bd93f9;
                color: #1e1e2e;
                border: 1px solid #f8f8f2;
            }
            QPushButton {
                background-color: #44475a;
                color: #f8f8f2;
                border: none;
                padding: 8px 12px;
                border-radius: 6px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #6272a4;
            }
            QPushButton:pressed {
                background-color: #3b3e58;
            }
            QLineEdit, QComboBox, QSpinBox {
                background-color: #3b3e58;
                border: 1px solid #44475a;
                padding: 5px;
                border-radius: 4px;
            }
            QTextEdit {
                background-color: #111;
                border: 1px solid #44475a;
                font-family: 'Consolas', 'Courier New', monospace;
            }
            QTabWidget::pane {
                border: 1px solid #44475a;
                background-color: #27293d;
            }
            QTabBar::tab {
                background: #27293d;
                border: 1px solid #44475a;
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                padding: 8px 12px;
                font-weight: bold;
            }
            QTabBar::tab:hover {
                background: #3b3e58;
            }
            QTabBar::tab:selected {
                background: #bd93f9;
                color: #1e1e2e;
            }
            QTableWidget {
                gridline-color: #44475a;
                background-color: #27293d;
            }
            QHeaderView::section {
                background-color: #3b3e58;
                color: #bd93f9;
                padding: 4px;
                border: 1px solid #44475a;
                font-weight: bold;
            }
            QScrollArea {
                border: none;
            }
        """)

    def on_tab_changed(self, index):
        self.global_active_tab_index = index
            
    @pyqtSlot(dict)
    def update_state(self, new_state):
        if new_state != self.FULL_STATE:
            self.FULL_STATE = new_state
            self.render_all_ui()

    @pyqtSlot(dict)
    def handle_villages_discovered(self, data):
        username = data.get('username')
        with state_lock:
            if 'village_data' not in self.FULL_STATE: self.FULL_STATE['village_data'] = {}
            self.FULL_STATE['village_data'][username] = data.get('villages', [])
        if username == self.selected_account_username: self.render_villages()

    @pyqtSlot(str, str)
    def update_log(self, level, message):
        color_map = {"INFO": "#8be9fd", "WARNING": "#f1fa8c", "ERROR": "#ff79c6", "CRITICAL": "#ff5555"}
        self.log_text_edit.append(f'<span style="color:{color_map.get(level, "#f8f8f2")};">{message}</span>')

    def render_all_ui(self):
        self.render_accounts(); self.render_villages()
        if self.selected_village_id: self.render_village_details()
        elif self.selected_account_username: self.render_account_settings()
        else: self.placeholder_widget.show(); self.details_stack.hide()

    def render_accounts(self):
        current_selection = self.selected_account_username
        self.account_list_widget.clear()
        
        selected_row = -1

        for i, acc in enumerate(self.FULL_STATE.get("accounts", [])):
            username = acc['username']
            is_active = acc.get('active', False)
            
            # Create a custom widget for the list item
            item_widget = QWidget()
            item_widget.setMinimumHeight(40) # Ensure a consistent, larger height
            
            # Main horizontal layout
            item_layout = QHBoxLayout(item_widget)
            item_layout.setContentsMargins(10, 5, 10, 5) # Add some padding
            item_layout.setSpacing(10)

            # Left side: Username and Status
            left_layout = QVBoxLayout()
            left_layout.setSpacing(0)
            username_label = QLabel(f"<b>{username}</b>")
            username_label.setTextFormat(Qt.TextFormat.RichText) # Ensure bold text is rendered
            status_label = QLabel("Running" if is_active else "Stopped")
            status_label.setStyleSheet(f"color: {'#50fa7b' if is_active else '#ff79c6'}; font-size: 11px;")
            left_layout.addWidget(username_label)
            left_layout.addWidget(status_label)
            
            # Right side: Buttons
            start_stop_btn = QPushButton("Stop" if is_active else "Start")
            start_stop_btn.setFixedWidth(80)
            start_stop_btn.clicked.connect(lambda _, u=username: self.toggle_account_status(u))
            
            remove_btn = QPushButton("X")
            remove_btn.setFixedWidth(35)
            remove_btn.setStyleSheet("background-color: #663333;") # Dark red for delete
            remove_btn.clicked.connect(lambda _, u=username: self.remove_account(u))
            
            # Assemble the layout
            item_layout.addLayout(left_layout)
            item_layout.addStretch() # This pushes the buttons to the right
            item_layout.addWidget(start_stop_btn)
            item_layout.addWidget(remove_btn)
            
            # Create the QListWidgetItem and set the custom widget
            list_item = QListWidgetItem()
            list_item.setSizeHint(item_widget.sizeHint())
            list_item.setData(Qt.ItemDataRole.UserRole, username)
            
            self.account_list_widget.addItem(list_item)
            self.account_list_widget.setItemWidget(list_item, item_widget)

            if username == current_selection:
                selected_row = i
        
        if selected_row != -1:
            self.account_list_widget.setCurrentRow(selected_row)
    
    def select_account(self, item):
        self.selected_account_username = item.data(Qt.ItemDataRole.UserRole); self.selected_village_id = None
        self.render_villages(); self.render_account_settings()

    def render_villages(self):
        current_selection_id = self.selected_village_id
        self.village_list_widget.clear()
        
        if not self.selected_account_username:
            return
            
        selected_row = -1

        villages = self.FULL_STATE.get("village_data", {}).get(self.selected_account_username, [])
        for i, village in enumerate(villages):
            item = QListWidgetItem(village['name'])
            item.setData(Qt.ItemDataRole.UserRole, village['id'])
            self.village_list_widget.addItem(item)
            
            if village['id'] == current_selection_id:
                selected_row = i

        if selected_row != -1:
            self.village_list_widget.setCurrentRow(selected_row)
            
    def select_village(self, item):
        if item and self.selected_village_id == item.data(Qt.ItemDataRole.UserRole):
            return

        self.selected_village_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.render_village_details()

    def render_account_settings(self):
        self.placeholder_widget.hide(); self.details_stack.show()
        self.village_title_label.setText(f"Settings for: {self.selected_account_username}")
        while self.tab_widget.count() > 0: self.tab_widget.removeTab(0)
        acc = next((a for a in self.FULL_STATE.get("accounts", []) if a['username'] == self.selected_account_username), None)
        if not acc: return
        settings_widget = QWidget(); form_layout = QFormLayout(settings_widget)
        password = QLineEdit(acc.get('password')); password.setEchoMode(QLineEdit.EchoMode.Password)
        server_url = QLineEdit(acc.get('server_url'))
        use_dual_queue = QCheckBox("Use Plus Account"); use_dual_queue.setChecked(acc.get('use_dual_queue', False))
        use_hero_resources = QCheckBox("Use Hero Resources"); use_hero_resources.setChecked(acc.get('use_hero_resources', False))
        proxy = acc.get('proxy', {}); proxy_ip = QLineEdit(proxy.get('ip')); proxy_port = QLineEdit(proxy.get('port'))
        proxy_user = QLineEdit(proxy.get('username')); proxy_pass = QLineEdit(proxy.get('password')); proxy_pass.setEchoMode(QLineEdit.EchoMode.Password)
        save_btn = QPushButton("Save Account Settings")
        form_layout.addRow("Password:", password); form_layout.addRow("Server URL:", server_url); form_layout.addRow(use_dual_queue); form_layout.addRow(use_hero_resources)
        form_layout.addRow(QLabel("<b>Proxy Settings</b>")); form_layout.addRow("IP:", proxy_ip); form_layout.addRow("Port:", proxy_port); form_layout.addRow("Username:", proxy_user); form_layout.addRow("Password:", proxy_pass)
        form_layout.addRow(save_btn)
        save_btn.clicked.connect(lambda: self.save_account_settings(self.selected_account_username,{'password': password.text(), 'server_url': server_url.text(),'use_dual_queue': use_dual_queue.isChecked(), 'use_hero_resources': use_hero_resources.isChecked(),'proxy': {'ip': proxy_ip.text(), 'port': proxy_port.text(), 'username': proxy_user.text(), 'password': proxy_pass.text()}}))
        self.tab_widget.addTab(settings_widget, "Account Settings")

    def render_village_details(self):
        if not self.selected_village_id:
            return

        current_tab_index = self.global_active_tab_index
        
        self.placeholder_widget.hide()
        self.details_stack.show()
        
        village_data = self.FULL_STATE.get("village_data", {}).get(str(self.selected_village_id))
        village_name = f"Village {self.selected_village_id} (Loading...)"
        
        if self.selected_account_username:
            account_villages = self.FULL_STATE.get("village_data",{}).get(self.selected_account_username, [])
            info = next((v for v in account_villages if v['id'] == self.selected_village_id), None)
            if info:
                village_name = info['name']
        
        self.village_title_label.setText(f"Details for: {village_name}")
        
        # Block signals while we repopulate to avoid triggering on_tab_changed
        self.tab_widget.blockSignals(True)
        while self.tab_widget.count() > 0:
            self.tab_widget.removeTab(0)
        
        if not village_data:
            # Unblock signals even if we return early
            self.tab_widget.blockSignals(False)
            return

        # Add all the tabs
        self.tab_widget.addTab(self._create_buildings_tab(village_data, "fields"), "Fields")
        self.tab_widget.addTab(self._create_buildings_tab(village_data, "city"), "City")
        self.tab_widget.addTab(self._create_queue_tab(), "Build Queue")
        self.tab_widget.addTab(self._create_training_tab(), "Training")
        self.tab_widget.addTab(self._create_smithy_tab(), "Smithy")
        self.tab_widget.addTab(self._create_demolish_tab(village_data), "Demolish")
        self.tab_widget.addTab(self._create_loop_tab(), "Loop")
        
        # Unblock signals *before* setting the index
        self.tab_widget.blockSignals(False)

        # Restore the correct tab
        if -1 < current_tab_index < self.tab_widget.count():
            self.tab_widget.setCurrentIndex(current_tab_index)

    def _create_buildings_tab(self, village_data, tab_type):
        container_widget = QWidget(); container_layout = QVBoxLayout(container_widget)
        if tab_type == 'fields':
            plan_group = QGroupBox("Resource Plan"); plan_layout = QHBoxLayout()
            plan_layout.addWidget(QLabel("Queue upgrades for all fields to level:")); plan_level_spin = QSpinBox()
            plan_level_spin.setMinimum(1); plan_level_spin.setMaximum(20)
            plan_btn = QPushButton("Set Plan"); plan_btn.clicked.connect(lambda: self.set_resource_plan(plan_level_spin.value()))
            plan_layout.addWidget(plan_level_spin); plan_layout.addWidget(plan_btn)
            plan_group.setLayout(plan_layout); container_layout.addWidget(plan_group)
        table = QTableWidget(); table.setColumnCount(4); table.setHorizontalHeaderLabels(["ID", "Name", "Level", "Actions"])
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        buildings = sorted([b for b in village_data.get('buildings', []) if (1 <= b['id'] <= 18 if tab_type == "fields" else b['id'] > 18)], key=lambda x: x['id'])
        table.setRowCount(len(buildings))
        for i, b in enumerate(buildings):
            table.setItem(i, 0, QTableWidgetItem(str(b['id']))); table.setItem(i, 1, QTableWidgetItem(GID_MAPPING.get(b.get('gid'), 'Unknown'))); table.setItem(i, 2, QTableWidgetItem(str(b.get('level', 0))))
            if b.get('gid', 0) > 0:
                action_widget = QWidget(); action_layout = QHBoxLayout(action_widget); action_layout.setContentsMargins(2, 2, 2, 2)
                level_spin = QSpinBox(); level_spin.setMinimum(b.get('level', 0) + 1); level_spin.setMaximum(100)
                queue_btn = QPushButton("Queue Lvl"); queue_btn.clicked.connect(lambda _, b=b, s=level_spin: self.queue_build(b, s.value()))
                action_layout.addWidget(level_spin); action_layout.addWidget(queue_btn); table.setCellWidget(i, 3, action_widget)
        container_layout.addWidget(table)
        return container_widget

    def _create_queue_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        vid_str = str(self.selected_village_id)

        # --- Template Controls ---
        templates_group = QGroupBox("Templates")
        templates_layout = QHBoxLayout(templates_group)
        self.template_name_input = QLineEdit()
        self.template_name_input.setPlaceholderText("Template Name")
        save_tpl_btn = QPushButton("Save"); save_tpl_btn.clicked.connect(self.save_template)
        self.template_dropdown = QComboBox(); self.populate_templates()
        load_tpl_btn = QPushButton("Load"); load_tpl_btn.clicked.connect(self.load_template)
        del_tpl_btn = QPushButton("Delete"); del_tpl_btn.clicked.connect(self.delete_template)
        templates_layout.addWidget(self.template_name_input); templates_layout.addWidget(save_tpl_btn)
        templates_layout.addWidget(QLabel("Load/Delete:"))
        templates_layout.addWidget(self.template_dropdown); templates_layout.addWidget(load_tpl_btn); templates_layout.addWidget(del_tpl_btn)
        
        # --- Queue List and Controls ---
        queue_group = QGroupBox("Build Queue")
        queue_layout = QHBoxLayout(queue_group)
        
        self.queue_list_widget = DraggableListWidget()
        self.queue_list_widget.setStyleSheet("QListWidget::item { padding: 8px; }")
        self.repopulate_queue_list() # Use the helper to fill the list

        # Vertical button layout for controls
        controls_layout = QVBoxLayout()
        move_top_btn = QPushButton("Top"); move_top_btn.clicked.connect(lambda: self.move_queue_item('top'))
        move_up_btn = QPushButton("Up"); move_up_btn.clicked.connect(lambda: self.move_queue_item('up'))
        move_down_btn = QPushButton("Down"); move_down_btn.clicked.connect(lambda: self.move_queue_item('down'))
        move_bottom_btn = QPushButton("Bottom"); move_bottom_btn.clicked.connect(lambda: self.move_queue_item('bottom'))
        remove_btn = QPushButton("Remove"); remove_btn.setStyleSheet("background-color: #ff5555;")
        remove_btn.clicked.connect(self.remove_queue_item)
        
        controls_layout.addWidget(move_top_btn)
        controls_layout.addWidget(move_up_btn)
        controls_layout.addWidget(move_down_btn)
        controls_layout.addWidget(move_bottom_btn)
        controls_layout.addStretch()
        controls_layout.addWidget(remove_btn)

        queue_layout.addWidget(self.queue_list_widget)
        queue_layout.addLayout(controls_layout)

        layout.addWidget(templates_group)
        layout.addWidget(queue_group)
        return widget
    
    def _create_training_tab(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); widget = QWidget(); layout = QVBoxLayout(widget)
        vid_str = str(self.selected_village_id); settings = self.FULL_STATE.get('training_queues', {}).get(vid_str, {})
        settings_group = QGroupBox("General Training Settings"); form = QFormLayout(settings_group)
        self.training_enabled_cb = QCheckBox("Enable Training Agent"); self.training_enabled_cb.setChecked(settings.get('enabled', False))
        self.min_queue_spin = QSpinBox(); self.min_queue_spin.setRange(0, 10000); self.min_queue_spin.setValue(settings.get('min_queue_duration_minutes', 15))
        self.max_time_edit = QLineEdit(settings.get('max_training_time', '')); self.max_time_edit.setPlaceholderText("dd.mm.yyyy hh:mm")
        form.addRow(self.training_enabled_cb); form.addRow("Min Queue (minutes):", self.min_queue_spin); form.addRow("End Time:", self.max_time_edit)
        layout.addWidget(settings_group)
        self.building_widgets = {}
        training_data = self.FULL_STATE.get('training_data', {}).get(vid_str, {}); building_map = {"barracks": (19, "Barracks"), "stable": (20, "Stable"), "workshop": (21, "Workshop"), "great_barracks": (29, "Great Barracks"), "great_stable": (30, "Great Stable")}
        for key, (gid, name) in building_map.items():
            if str(gid) in training_data:
                building_group = QGroupBox(name); building_group.setCheckable(True); b_settings = settings.get('buildings', {}).get(key, {})
                building_group.setChecked(b_settings.get('enabled', False)); b_layout = QFormLayout(building_group)
                troop_combo = QComboBox()
                for troop in training_data.get(str(gid), {}).get('trainable', []): troop_combo.addItem(troop['name'])
                if b_settings.get('troop_name'): troop_combo.setCurrentText(b_settings.get('troop_name'))
                b_layout.addRow("Troop to Train:", troop_combo); layout.addWidget(building_group)
                self.building_widgets[key] = {'group': building_group, 'combo': troop_combo}
        save_btn = QPushButton("Save Training Settings"); save_btn.clicked.connect(self.save_training_settings); layout.addWidget(save_btn)
        scroll.setWidget(widget); return scroll
        
    def _create_smithy_tab(self):
        widget = QWidget(); layout = QVBoxLayout(widget); vid_str = str(self.selected_village_id)
        smithy_data = self.FULL_STATE.get('smithy_data', {}).get(vid_str)
        if not smithy_data: layout.addWidget(QLabel("No smithy data available.")); return widget
        settings = self.FULL_STATE.get('smithy_upgrades', {}).get(vid_str, {})
        self.smithy_enabled_cb = QCheckBox("Enable Auto Upgrades"); self.smithy_enabled_cb.setChecked(settings.get('enabled', False))
        self.smithy_priority_list = DraggableListWidget(); self.smithy_priority_list.setToolTip("Drag and drop to set upgrade priority.")
        priority_names = set(settings.get('priority', [])); all_researches = smithy_data.get('researches', [])
        for name in settings.get('priority', []):
            research = next((r for r in all_researches if r['name'] == name), None)
            if research: self.smithy_priority_list.addItem(f"{research['name']} (Level {research['level']})")
        for research in all_researches:
            if research['name'] not in priority_names: self.smithy_priority_list.addItem(f"{research['name']} (Level {research['level']})")
        save_btn = QPushButton("Save Smithy Settings"); save_btn.clicked.connect(self.save_smithy_settings)
        layout.addWidget(self.smithy_enabled_cb); layout.addWidget(QLabel("<b>Upgrade Priority</b>")); layout.addWidget(self.smithy_priority_list); layout.addWidget(save_btn)
        return widget
        
    def _create_demolish_tab(self, village_data):
        widget = QWidget(); layout = QVBoxLayout(widget); vid_str = str(self.selected_village_id)
        buildings_to_show = sorted([b for b in village_data.get('buildings', []) if b['id'] > 18 and b.get('level', 0) > 0], key=lambda b: GID_MAPPING.get(b.get('gid'), 'Z'))
        table = QTableWidget(); table.setColumnCount(3); table.setHorizontalHeaderLabels(["Name", "Level", "Action"]); table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.setRowCount(len(buildings_to_show))
        for i, b in enumerate(buildings_to_show):
            table.setItem(i, 0, QTableWidgetItem(GID_MAPPING.get(b.get('gid'), 'Unknown'))); table.setItem(i, 1, QTableWidgetItem(str(b.get('level', 0))))
            queue_demolish_btn = QPushButton("Queue Demolition"); queue_demolish_btn.clicked.connect(lambda _, b=b: self.queue_demolish(b))
            table.setCellWidget(i, 2, queue_demolish_btn)
        layout.addWidget(table); layout.addWidget(QLabel("<b>Demolition Queue</b>"))
        demolish_queue_list = QListWidget()
        for task in self.FULL_STATE.get('demolish_queues', {}).get(vid_str, []): demolish_queue_list.addItem(f"Demolish {GID_MAPPING.get(task.get('gid'), '?')} to {task.get('level')}")
        layout.addWidget(demolish_queue_list); return widget

    def _create_loop_tab(self):
        widget = QWidget(); layout = QVBoxLayout(widget); vid_str = str(self.selected_village_id)
        settings = self.FULL_STATE.get('loop_module_state', {}).get(vid_str, {})
        
        settings_group = QGroupBox("Loop Settings"); form = QFormLayout(settings_group)
        self.loop_enabled_cb = QCheckBox("Enable Settling & Destruction Loop"); self.loop_enabled_cb.setChecked(settings.get('enabled', False))
        self.catapult_origin_combo = QComboBox()
        if self.selected_account_username:
            for v in self.FULL_STATE.get("village_data", {}).get(self.selected_account_username, []): self.catapult_origin_combo.addItem(v['name'], v['id'])
        if settings.get('catapult_origin_village'):
            index = self.catapult_origin_combo.findData(int(settings['catapult_origin_village']))
            if index != -1:
                self.catapult_origin_combo.setCurrentIndex(index)
        save_btn = QPushButton("Save Loop Settings"); save_btn.clicked.connect(self.save_loop_settings)
        form.addRow(self.loop_enabled_cb); form.addRow("Catapult Origin:", self.catapult_origin_combo); form.addRow(save_btn)
        
        status_group = QGroupBox("Loop Status")
        status_layout = QVBoxLayout(status_group)
        for i, slot in enumerate(settings.get('settlement_slots', [])):
            village_name_str = ""
            if slot.get('new_village_id'):
                for acc_v in self.FULL_STATE.get('village_data', {}).values():
                    v_info = next((v for v in acc_v if v['id'] == slot['new_village_id']), None)
                    if v_info: village_name_str = f" (Managing: <b>{v_info['name']}</b>)"; break
            status_layout.addWidget(QLabel(f"Slot {i+1}: <b style='color:#f1fa8c;'>{slot.get('status', 'unknown')}</b>{village_name_str}"))

        layout.addWidget(settings_group); layout.addWidget(status_group); return widget

    def save_template(self):
        name = self.template_name_input.text().strip()
        if not name or not self.selected_village_id: return
        with state_lock:
            BOT_STATE.setdefault('build_templates', {})[name] = BOT_STATE.get('build_queues', {}).get(str(self.selected_village_id), [])
        save_config(); log.info(f"Saved template '{name}'"); self.populate_templates()

    def load_template(self):
        name = self.template_dropdown.currentText()
        if not name or not self.selected_village_id: return
        vid_str = str(self.selected_village_id)
        with state_lock:
            template_queue = BOT_STATE.get('build_templates', {}).get(name)
            if template_queue is not None: BOT_STATE['build_queues'][vid_str] = copy.deepcopy(template_queue)
        save_config(); log.info(f"Loaded template '{name}' to village {vid_str}"); self.repopulate_queue_list()
    
    def delete_template(self):
        name = self.template_dropdown.currentText()
        if not name or QMessageBox.question(self, 'Confirm', f"Delete template '{name}'?") != QMessageBox.StandardButton.Yes: return
        with state_lock: BOT_STATE.get('build_templates', {}).pop(name, None)
        save_config(); log.info(f"Deleted template '{name}'"); self.populate_templates()

    def populate_templates(self):
        if hasattr(self, 'template_dropdown'):
            self.template_dropdown.clear()
            for name in self.FULL_STATE.get('build_templates', {}).keys(): self.template_dropdown.addItem(name)

    def repopulate_queue_list(self):
        if hasattr(self, 'queue_list_widget'):
            # Save the current selection's index
            current_index = self.queue_list_widget.currentRow()

            self.queue_list_widget.clear()
            queue = self.FULL_STATE.get('build_queues', {}).get(str(self.selected_village_id), [])
            
            for i, task in enumerate(queue):
                name = f"ALL RESOURCES to Lvl {task['level']}" if task.get('type') == 'resource_plan' else f"{GID_MAPPING.get(task.get('gid'), '?')} (Loc:{task.get('location', '??')}) to Lvl {task['level']}"
                self.queue_list_widget.addItem(f"{i+1}. {name}")
            
            # Restore the selection if it's still valid
            if 0 <= current_index < self.queue_list_widget.count():
                self.queue_list_widget.setCurrentRow(current_index)

    def save_training_settings(self):
        vid_str = str(self.selected_village_id)
        settings = {'enabled': self.training_enabled_cb.isChecked(),'min_queue_duration_minutes': self.min_queue_spin.value(),'max_training_time': self.max_time_edit.text(),'buildings': {}}
        for key, widgets in self.building_widgets.items():
            settings['buildings'][key] = {'enabled': widgets['group'].isChecked(), 'troop_name': widgets['combo'].currentText()}
        with state_lock: BOT_STATE.setdefault('training_queues', {})[vid_str] = settings
        save_config(); log.info(f"Saved training settings for village {vid_str}")

    def save_smithy_settings(self):
        vid = str(self.selected_village_id)
        enabled = self.smithy_enabled_cb.isChecked()
        priority = [self.smithy_priority_list.item(i).text().split(' (')[0] for i in range(self.smithy_priority_list.count())]
        with state_lock: BOT_STATE.setdefault('smithy_upgrades', {})[vid] = {'enabled': enabled, 'priority': priority}
        save_config(); log.info(f"Saved smithy settings for village {vid}")
        
    def save_loop_settings(self):
        vid = str(self.selected_village_id)
        enabled = self.loop_enabled_cb.isChecked()
        catapult_id = self.catapult_origin_combo.currentData()
        with state_lock: BOT_STATE.setdefault('loop_module_state', {}).setdefault(vid, {})['enabled'] = enabled
        with state_lock: BOT_STATE.setdefault('loop_module_state', {})[vid]['catapult_origin_village'] = catapult_id
        save_config(); log.info(f"Saved loop settings for village {vid}")
        
    def queue_demolish(self, building):
        vid = str(self.selected_village_id)
        task = {'type': 'demolish', 'location': building['id'], 'gid': building['gid'], 'level': building['level'] - 1}
        with state_lock: BOT_STATE.setdefault('demolish_queues', {}).setdefault(vid, []).append(task)
        save_config(); log.info(f"Queued demolition for {GID_MAPPING.get(building['gid'])} in village {vid}")

    def add_account(self):
        username = self.username_input.text().strip()
        if not username: QMessageBox.warning(self, "Input Error", "Username is required."); return
        with state_lock:
            if any(a['username'].lower() == username.lower() for a in BOT_STATE['accounts']): QMessageBox.warning(self, "Duplicate Error", "Account already exists."); return
            BOT_STATE['accounts'].append({"username": username, "password": self.password_input.text(), "server_url": self.server_url_input.text().strip(), "is_sitter": self.is_sitter_checkbox.isChecked(), "sitter_for": self.sitter_for_input.text().strip(),"login_username": username, "tribe": "roman","use_dual_queue": self.use_dual_queue_checkbox.isChecked(),"use_hero_resources": self.use_hero_resources_checkbox.isChecked(),"building_logic": "default", "active": False,"proxy": {'ip': self.proxy_ip_input.text(), 'port': self.proxy_port_input.text(), 'username': self.proxy_user_input.text(), 'password': self.proxy_pass_input.text()}})
        save_config(); log.info(f"Added new account: {username}"); self.render_accounts()

    def save_account_settings(self, username, settings):
        with state_lock:
            for i, acc in enumerate(BOT_STATE['accounts']):
                if acc['username'] == username: BOT_STATE['accounts'][i].update(settings); break
        save_config(); log.info(f"Updated settings for account {username}")
        QMessageBox.information(self, "Success", "Account settings have been saved.")

    def toggle_account_status(self, username):
        with state_lock:
            for acc in BOT_STATE['accounts']:
                if acc['username'] == username: acc['active'] = not acc.get('active', False); break
        save_config(); self.render_accounts()

    def remove_account(self, username):
        if QMessageBox.question(self, 'Confirm', f"Remove account '{username}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            with state_lock: BOT_STATE['accounts'] = [a for a in BOT_STATE['accounts'] if a['username'] != username]
            save_config(); log.info(f"Removed account: {username}"); self.render_all_ui()

    def set_resource_plan(self, level): self.queue_task({'type': 'resource_plan', 'level': level})
    def queue_build(self, building_info, level): self.queue_task({'type': 'building', 'location': building_info['id'], 'gid': building_info['gid'], 'level': level})
    def queue_task(self, task):
        if not self.selected_village_id: return
        vid = str(self.selected_village_id)
        with state_lock: BOT_STATE.setdefault('build_queues', {}).setdefault(vid, []).append(task)
        save_config(); log.info(f"Task queued for village {vid}: {task}")

    def closeEvent(self, event):
        log.info("Closing application..."); self.bot_manager.stop(); self.bot_manager.join(5); event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())