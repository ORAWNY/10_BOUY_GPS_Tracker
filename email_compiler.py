import os
import sqlite3
import win32com.client
import re

def export_emails_to_db_folder(mailbox_name, folder_path_list, base_output_folder="Logger_Data"):
    if not os.path.exists(base_output_folder):
        os.makedirs(base_output_folder)

    # Use ONE single DB for all folders
    db_path = os.path.join(base_output_folder, "Logger_Data.db")
    print(f"Exporting emails from {' > '.join(folder_path_list)} to {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    num_data_cols = 18
    col_names = []
    for i in range(1, num_data_cols + 1):
        if i == 5:
            col_names.append("Volt")
        elif i == 6:
            col_names.append("Bat1")
        elif i == 7:
            col_names.append("Bat2")
        elif i == 8:
            col_names.append("Bat3")
        elif i == 11:
            col_names.append("Lat")
        elif i == 12:
            col_names.append("Lon")
        else:
            col_names.append(f"col{i}")

    columns_sql = ",\n".join([f"{col} TEXT" for col in col_names])
    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            sender TEXT,
            received_time TEXT,
            {columns_sql}
        )
    """
    cursor.execute(create_table_sql)

    # Create a unique table name by joining folder names and replacing spaces with underscores
    table_name = "_".join([name.replace(" ", "_") for name in folder_path_list])
    # SQLite table names should be safe; still, let's sanitize just in case (remove problematic chars)
    table_name = re.sub(r'\W+', '_', table_name)

    columns_sql = ",\n".join([f"{col} TEXT" for col in col_names])
    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INTEGER PRIMARY KEY,   -- removed AUTOINCREMENT here
            subject TEXT,
            sender TEXT,
            received_time TEXT,
            {columns_sql}
        )
    """
    cursor.execute(create_table_sql)
    conn.commit()

    outlook = win32com.client.Dispatch('Outlook.Application').GetNamespace("MAPI")

    try:
        mailbox = outlook.Folders.Item(mailbox_name)
    except Exception as e:
        print(f"Mailbox '{mailbox_name}' not found: {e}")
        conn.close()
        return

    folder = mailbox
    try:
        for subfolder_name in folder_path_list:
            folder = folder.Folders[subfolder_name]
    except Exception as e:
        print(f"Folder path {' > '.join(folder_path_list)} not found: {e}")
        conn.close()
        return

    messages = folder.Items
    print(f"Found {messages.Count} emails in {' > '.join(folder_path_list)}")

    csv_line_pattern = re.compile(r"\[[a-zA-Z]\d\]#S,(.*)")

    count_inserted = 0
    count_skipped = 0

    for msg in messages:
        try:
            subject = msg.Subject if msg.Subject else ""
            sender = msg.SenderName if msg.SenderName else ""
            try:
                received_time = msg.ReceivedTime.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                received_time = ""

            body = msg.Body if msg.Body else ""

            csv_match = None
            for line in body.splitlines():
                match = csv_line_pattern.match(line.strip())
                if match:
                    csv_match = match.group(1)
                    break

            if csv_match is None:
                data_cols = [""] * num_data_cols
            else:
                data_cols = csv_match.split(",")
                if len(data_cols) < num_data_cols:
                    data_cols += [""] * (num_data_cols - len(data_cols))
                else:
                    data_cols = data_cols[:num_data_cols]

            # Check if exact data row already exists:
            query = f"""
                SELECT 1 FROM {table_name} WHERE
                subject = ? AND sender = ? AND received_time = ? AND
                {" AND ".join([f"{col} = ?" for col in col_names])}
                LIMIT 1
            """
            cursor.execute(query, (subject, sender, received_time, *data_cols))
            if cursor.fetchone():
                count_skipped += 1
                continue

            cursor.execute(f"""
                INSERT INTO {table_name} (subject, sender, received_time, {', '.join(col_names)})
                VALUES (?, ?, ?, {', '.join(['?'] * num_data_cols)})
            """, (subject, sender, received_time, *data_cols))
            count_inserted += 1

        except Exception as e:
            print(f"Failed to save an email: {e}")

    conn.commit()
    conn.close()

    print(f"Inserted {count_inserted} new emails into table '{table_name}' in {db_path}")
    if count_skipped > 0:
        print(f"Skipped {count_skipped} emails due to duplicate data")

if __name__ == "__main__":
    mailbox_name = "metocean configuration"

    folder_paths = [
        ["Inbox", "Logger Data", "SEAI", "L8"],
        ["Inbox", "Logger Data", "TGS - Bravo", "L8"],
        ["Inbox", "Logger Data", "TGS - ALPHA", "L8"],
        ["Inbox", "Logger Data", "Magnora", "L8"],
    ]

    for folder_path in folder_paths:
        export_emails_to_db_folder(mailbox_name, folder_path)
