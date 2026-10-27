
import getpass
import sqlite3

from werkzeug.security import generate_password_hash
from app import DATABASE, init_db


def main():
    init_db()

    matric = input("Enter the admin username/matric ID: ").strip()
    password = getpass.getpass("Enter a strong admin password: ")

    if not matric or len(password) < 12:
        print("Enter a username and a password of at least 12 characters.")
        return

    confirm = getpass.getpass("Confirm the password: ")

    if password != confirm:
        print("Passwords do not match.")
        return

    db = sqlite3.connect(DATABASE)

    try:
        db.execute("""
            INSERT INTO users
            (full_name, matric_number, department, level,
             password_hash, role)
            VALUES (?, ?, ?, ?, ?, 'admin')
        """, (
            "Hostel Administrator",
            matric,
            "Administration",
            "Staff",
            generate_password_hash(password)
        ))

        db.commit()
        print("Administrator account created successfully.")

    except sqlite3.IntegrityError:
        db.rollback()
        print("That username already exists. Choose another username.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
