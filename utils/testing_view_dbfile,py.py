import sys
import sqlite3
import pandas as pd
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QListWidget,
    QTableWidget, QTableWidgetItem, QMessageBox, QSplitter
)
from PyQt6.QtCore import Qt

DB_PATH = r"D:\04_Met_Ocean\02_Python\10_BOUY_GPS_Tracker\Logger_Data\Logger_data_all_projects.db"

class DBViewer(QWidget):
    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path
        self.conn = None
        self.init_ui()
        self.load_tables()

    def init_ui(self):
        self.setWindowTitle("SQLite DB Viewer")

        layout = QVBoxLayout()
        self.setLayout(layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # List widget to show tables
        self.table_list = QListWidget()
        self.table_list.clicked.connect(self.load_table_preview)
        splitter.addWidget(self.table_list)

        # Table widget to show preview of data
        self.data_preview = QTableWidget()
        splitter.addWidget(self.data_preview)

        splitter.setSizes([150, 600])

    def connect_db(self):
        if self.conn is None:
            try:
                self.conn = sqlite3.connect(self.db_path)
            except Exception as e:
                QMessageBox.critical(self, "Connection Error", str(e))

    def load_tables(self):
        self.connect_db()
        if self.conn:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
                tables = cursor.fetchall()
                self.table_list.clear()
                for table in tables:
                    self.table_list.addItem(table[0])
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not retrieve tables:\n{e}")

    def load_table_preview(self):
        table_name = self.table_list.currentItem().text()
        try:
            query = f"SELECT * FROM {table_name} LIMIT 10"
            df = pd.read_sql_query(query, self.conn)
            self.show_data_preview(df)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load data:\n{e}")

    def show_data_preview(self, df):
        self.data_preview.clear()
        self.data_preview.setRowCount(len(df))
        self.data_preview.setColumnCount(len(df.columns))
        self.data_preview.setHorizontalHeaderLabels(df.columns)

        for i, row in df.iterrows():
            for j, val in enumerate(row):
                item = QTableWidgetItem(str(val))
                self.data_preview.setItem(i, j, item)
        self.data_preview.resizeColumnsToContents()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    viewer = DBViewer(DB_PATH)
    viewer.resize(800, 600)
    viewer.show()
    sys.exit(app.exec())
