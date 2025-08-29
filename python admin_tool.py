import os
import pymysql
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class AdminTool:
    def __init__(self):
        self.connection = None
        self.connect()
    
    def connect(self):
        try:
            self.connection = pymysql.connect(
                host=os.getenv('DB_HOST', 'localhost'),
                user=os.getenv('DB_USER', 'lomitalk'),
                password=os.getenv('DB_PASSWORD', ''),
                database=os.getenv('DB_NAME', 'lomitalk'),
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor
            )
            print("✅ Connected to MySQL database")
        except pymysql.MySQLError as e:
            print(f"❌ Error connecting to MySQL: {e}")
            raise
    
    def ensure_connection(self):
        try:
            self.connection.ping(reconnect=True)
        except pymysql.MySQLError:
            self.connect()
    
    def add_points(self, user_identifier, amount, reason, admin_id):
        self.ensure_connection()
        try:
            with self.connection.cursor() as cursor:
                # Find user by nickname or Telegram ID
                if user_identifier.isdigit():
                    sql = "SELECT * FROM users WHERE telegram_id = %s"
                    cursor.execute(sql, (int(user_identifier),))
                else:
                    sql = "SELECT * FROM users WHERE nickname = %s"
                    cursor.execute(sql, (user_identifier,))
                
                user = cursor.fetchone()
                if not user:
                    print("❌ User not found")
                    return False
                
                # Add transaction
                sql = """
                INSERT INTO transactions (user_id, amount, type, description, admin_id)
                VALUES (%s, %s, 'deposit', %s, %s)
                """
                cursor.execute(sql, (user['id'], amount, reason, admin_id))
                
                # Update user points
                sql = "UPDATE users SET points = points + %s WHERE id = %s"
                cursor.execute(sql, (amount, user['id']))
                
                self.connection.commit()
                print(f"✅ Added {amount} points to {user['nickname']}")
                return True
        except pymysql.MySQLError as e:
            print(f"❌ Error adding points: {e}")
            return False
    
    def remove_points(self, user_identifier, amount, reason, admin_id):
        self.ensure_connection()
        try:
            with self.connection.cursor() as cursor:
                # Find user by nickname or Telegram ID
                if user_identifier.isdigit():
                    sql = "SELECT * FROM users WHERE telegram_id = %s"
                    cursor.execute(sql, (int(user_identifier),))
                else:
                    sql = "SELECT * FROM users WHERE nickname = %s"
                    cursor.execute(sql, (user_identifier,))
                
                user = cursor.fetchone()
                if not user:
                    print("❌ User not found")
                    return False
                
                # Check if user has enough points
                if user['points'] < amount:
                    print(f"❌ User only has {user['points']} points")
                    return False
                
                # Add transaction
                sql = """
                INSERT INTO transactions (user_id, amount, type, description, admin_id)
                VALUES (%s, %s, 'withdrawal', %s, %s)
                """
                cursor.execute(sql, (user['id'], -amount, reason, admin_id))
                
                # Update user points
                sql = "UPDATE users SET points = points - %s WHERE id = %s"
                cursor.execute(sql, (amount, user['id']))
                
                self.connection.commit()
                print(f"✅ Removed {amount} points from {user['nickname']}")
                return True
        except pymysql.MySQLError as e:
            print(f"❌ Error removing points: {e}")
            return False
    
    def view_user(self, user_identifier):
        self.ensure_connection()
        try:
            with self.connection.cursor() as cursor:
                # Find user by nickname or Telegram ID
                if user_identifier.isdigit():
                    sql = "SELECT * FROM users WHERE telegram_id = %s"
                    cursor.execute(sql, (int(user_identifier),))
                else:
                    sql = "SELECT * FROM users WHERE nickname = %s"
                    cursor.execute(sql, (user_identifier,))
                
                user = cursor.fetchone()
                if not user:
                    print("❌ User not found")
                    return False
                
                print(f"\n👤 User Details:")
                print(f"ID: {user['id']}")
                print(f"Telegram ID: {user['telegram_id']}")
                print(f"Username: @{user['username']}")
                print(f"Nickname: {user['nickname']}")
                print(f"Full Name: {user['full_name']}")
                print(f"Sex: {user['sex']}")
                print(f"Age Group: {user['age_group']}")
                print(f"Points: {user['points']}")
                print(f"Status: {user['status']}")
                print(f"Conversations: {user['conversation_count']}")
                print(f"Total Characters: {user['total_chars']}")
                print(f"Joined: {user['created_at']}")
                
                return True
        except pymysql.MySQLError as e:
            print(f"❌ Error viewing user: {e}")
            return False
    
    def view_reports(self, status='pending'):
        self.ensure_connection()
        try:
            with self.connection.cursor() as cursor:
                sql = """
                SELECT r.*, u1.nickname as reporter_name, u2.nickname as reported_name 
                FROM reports r
                JOIN users u1 ON r.reporter_id = u1.id
                JOIN users u2 ON r.reported_user_id = u2.id
                WHERE r.status = %s
                ORDER BY r.created_at DESC
                LIMIT 20
                """
                cursor.execute(sql, (status,))
                reports = cursor.fetchall()
                
                if not reports:
                    print("No reports found.")
                    return False
                
                print(f"\n⚠️ Reports (Status: {status}):")
                for report in reports:
                    print(f"\nID: {report['id']}")
                    print(f"From: {report['reporter_name']}")
                    print(f"Against: {report['reported_name']}")
                    print(f"Reason: {report['reason']}")
                    print(f"Status: {report['status']}")
                    print(f"Date: {report['created_at']}")
                    print("-" * 30)
                
                return True
        except pymysql.MySQLError as e:
            print(f"❌ Error viewing reports: {e}")
            return False

def main():
    tool = AdminTool()
    
    while True:
        print("\n👮 Admin Management Tool")
        print("1. Add points to user")
        print("2. Remove points from user")
        print("3. View user details")
        print("4. View pending reports")
        print("5. Exit")
        
        choice = input("\nEnter your choice: ").strip()
        
        if choice == '1':
            user_id = input("Enter user nickname or Telegram ID: ").strip()
            amount = input("Enter amount to add: ").strip()
            reason = input("Enter reason: ").strip()
            admin_id = input("Enter your admin ID: ").strip()
            
            if not user_id or not amount or not reason or not admin_id:
                print("❌ All fields are required")
                continue
            
            try:
                amount = int(amount)
                admin_id = int(admin_id)
            except ValueError:
                print("❌ Amount and admin ID must be numbers")
                continue
            
            tool.add_points(user_id, amount, reason, admin_id)
        
        elif choice == '2':
            user_id = input("Enter user nickname or Telegram ID: ").strip()
            amount = input("Enter amount to remove: ").strip()
            reason = input("Enter reason: ").strip()
            admin_id = input("Enter your admin ID: ").strip()
            
            if not user_id or not amount or not reason or not admin_id:
                print("❌ All fields are required")
                continue
            
            try:
                amount = int(amount)
                admin_id = int(admin_id)
            except ValueError:
                print("❌ Amount and admin ID must be numbers")
                continue
            
            tool.remove_points(user_id, amount, reason, admin_id)
        
        elif choice == '3':
            user_id = input("Enter user nickname or Telegram ID: ").strip()
            if not user_id:
                print("❌ User identifier is required")
                continue
            
            tool.view_user(user_id)
        
        elif choice == '4':
            tool.view_reports()
        
        elif choice == '5':
            print("👋 Goodbye!")
            break
        
        else:
            print("❌ Invalid choice")

if __name__ == "__main__":
    main()