import sqlite3
import logging
import random
from typing import Dict, List, Optional, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path: str = 'lomitalk.db'):
        self.db_path = db_path
        self.conn = None
        self.connect()
        self.setup_tables()
    
    def connect(self):
        """Connect to SQLite database"""
        try:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.row_factory = sqlite3.Row  # Return rows as dictionaries
            logger.info("✅ Connected to SQLite database")
        except sqlite3.Error as e:
            logger.error(f"❌ Database connection error: {e}")
            raise
    
    def setup_tables(self):
        """Create all necessary tables with simplified syntax"""
        try:
            cursor = self.conn.cursor()
            
            # Users table - SIMPLIFIED
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                nickname TEXT NOT NULL,
                full_name TEXT,
                sex TEXT DEFAULT 'other',
                age_group TEXT DEFAULT '25-30',
                points INTEGER DEFAULT 1000,
                status TEXT DEFAULT 'inactive',
                partner_id INTEGER,
                conversation_count INTEGER DEFAULT 0,
                total_chars INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            
            # Conversations table - SIMPLIFIED
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                partner_id INTEGER NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                client_char_count INTEGER DEFAULT 0,
                points_transferred INTEGER DEFAULT 0
            )
            """)
            
            # Messages table - SIMPLIFIED
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                message_text TEXT,
                char_count INTEGER,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            
            # Transactions table - SIMPLIFIED
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                type TEXT NOT NULL,
                description TEXT,
                admin_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            
            # Reports table - SIMPLIFIED
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                reported_user_id INTEGER NOT NULL,
                reason TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            
            self.conn.commit()
            logger.info("✅ Database tables created successfully")
            
        except sqlite3.Error as e:
            logger.error(f"❌ Error creating tables: {e}")
            raise
    
    def generate_nickname(self, telegram_id: int) -> str:
        """Generate a random nickname"""
        return f"user{random.randint(1000, 9999)}"
    
    # User management methods
    def get_user(self, telegram_id: int) -> Optional[Dict]:
        """Get user by Telegram ID"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"Error getting user {telegram_id}: {e}")
            return None
    
    def create_user(self, user_data: Dict) -> bool:
        """Create a new user"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
            INSERT INTO users (telegram_id, username, nickname, full_name, sex, age_group, points)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                user_data.get('telegram_id'),
                user_data.get('username', 'N/A'),
                user_data.get('nickname', self.generate_nickname(user_data.get('telegram_id'))),
                user_data.get('full_name', ''),
                user_data.get('sex', 'other'),
                user_data.get('age_group', '25-30'),
                user_data.get('points', 1000)
            ))
            self.conn.commit()
            logger.info(f"✅ User created: {user_data.get('telegram_id')}")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error creating user: {e}")
            return False
    
    def update_user(self, telegram_id: int, updates: Dict) -> bool:
        """Update user information"""
        try:
            cursor = self.conn.cursor()
            set_clause = ", ".join([f"{key} = ?" for key in updates.keys()])
            values = list(updates.values()) + [telegram_id]
            cursor.execute(f"UPDATE users SET {set_clause} WHERE telegram_id = ?", values)
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error updating user {telegram_id}: {e}")
            return False
    
    # Conversation management
    def start_conversation(self, client_id: int, partner_id: int) -> Optional[int]:
        """Start a new conversation between two users"""
        try:
            cursor = self.conn.cursor()
            
            # Get user IDs
            client_user_id = self.get_user_id(client_id)
            partner_user_id = self.get_user_id(partner_id)
            
            if not client_user_id or not partner_user_id:
                return None
            
            cursor.execute("""
            INSERT INTO conversations (client_id, partner_id)
            VALUES (?, ?)
            """, (client_user_id, partner_user_id))
            
            # Update users status
            cursor.execute("UPDATE users SET status = 'busy', partner_id = ? WHERE telegram_id = ?", (partner_id, client_id))
            cursor.execute("UPDATE users SET status = 'busy', partner_id = ? WHERE telegram_id = ?", (client_id, partner_id))
            
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.Error as e:
            logger.error(f"Error starting conversation: {e}")
            return None
    
    def end_conversation(self, conversation_id: int, char_count: int) -> bool:
        """End a conversation and transfer points"""
        try:
            cursor = self.conn.cursor()
            
            # Get conversation details
            cursor.execute("""
            SELECT c.*, u1.telegram_id as client_tg, u2.telegram_id as partner_tg, 
                   u1.points as client_points, u2.points as partner_points
            FROM conversations c
            JOIN users u1 ON c.client_id = u1.id
            JOIN users u2 ON c.partner_id = u2.id
            WHERE c.id = ?
            """, (conversation_id,))
            
            conv = cursor.fetchone()
            if not conv:
                return False
            
            conv_dict = dict(conv)
            
            # Calculate points to transfer (ensure client has enough points)
            points_to_transfer = min(char_count, conv_dict['client_points'])
            
            # Update points
            cursor.execute("UPDATE users SET points = points - ? WHERE id = ?", (points_to_transfer, conv_dict['client_id']))
            cursor.execute("UPDATE users SET points = points + ? WHERE id = ?", (points_to_transfer, conv_dict['partner_id']))
            
            # End conversation and update stats
            cursor.execute("""
            UPDATE conversations 
            SET ended_at = CURRENT_TIMESTAMP, client_char_count = ?, points_transferred = ?
            WHERE id = ?
            """, (char_count, points_to_transfer, conversation_id))
            
            cursor.execute("""
            UPDATE users 
            SET status = 'inactive', partner_id = NULL, 
                conversation_count = conversation_count + 1,
                total_chars = total_chars + ?
            WHERE id IN (?, ?)
            """, (char_count, conv_dict['client_id'], conv_dict['partner_id']))
            
            # Record transaction
            cursor.execute("""
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, 'conversation', 'Points transferred for conversation')
            """, (conv_dict['client_id'], -points_to_transfer))
            
            cursor.execute("""
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, 'conversation', 'Points received from conversation')
            """, (conv_dict['partner_id'], points_to_transfer))
            
            self.conn.commit()
            return True
            
        except sqlite3.Error as e:
            logger.error(f"Error ending conversation: {e}")
            return False
    
    # Partner matching
    def find_available_partners(self, exclude_telegram_id: int, sex: str = None, age_group: str = None) -> List[Dict]:
        """Find available partners with optional filters"""
        try:
            cursor = self.conn.cursor()
            query = """
            SELECT * FROM users 
            WHERE status = 'active' 
            AND partner_id IS NULL 
            AND telegram_id != ?
            """
            params = [exclude_telegram_id]
            
            if sex and sex != 'Any':
                query += " AND sex = ?"
                params.append(sex.lower())
            
            if age_group and age_group != 'Any':
                query += " AND age_group = ?"
                params.append(age_group)
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error finding partners: {e}")
            return []
    
    # Message logging
    def log_message(self, conversation_id: int, sender_id: int, message_text: str) -> bool:
        """Log a message for moderation"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
            INSERT INTO messages (conversation_id, sender_id, message_text, char_count)
            VALUES (?, ?, ?, ?)
            """, (conversation_id, sender_id, message_text, len(message_text)))
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error logging message: {e}")
            return False
    
    # Admin functions
    def add_points(self, telegram_id: int, amount: int, description: str, admin_id: int = None) -> bool:
        """Add or remove points from a user"""
        try:
            cursor = self.conn.cursor()
            
            # Update user points
            cursor.execute("UPDATE users SET points = points + ? WHERE telegram_id = ?", (amount, telegram_id))
            
            # Record transaction
            transaction_type = 'deposit' if amount > 0 else 'withdrawal'
            cursor.execute("""
            INSERT INTO transactions (user_id, amount, type, description, admin_id)
            VALUES ((SELECT id FROM users WHERE telegram_id = ?), ?, ?, ?, ?)
            """, (telegram_id, amount, transaction_type, description, admin_id))
            
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adjusting points: {e}")
            return False
    
    def create_report(self, reporter_id: int, reported_id: int, reason: str) -> bool:
        """Create a user report"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
            INSERT INTO reports (reporter_id, reported_user_id, reason)
            VALUES (
                (SELECT id FROM users WHERE telegram_id = ?),
                (SELECT id FROM users WHERE telegram_id = ?),
                ?
            )
            """, (reporter_id, reported_id, reason))
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error creating report: {e}")
            return False
    
    # Utility methods
    def get_user_id(self, telegram_id: int) -> Optional[int]:
        """Get internal user ID from Telegram ID"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            result = cursor.fetchone()
            return result['id'] if result else None
        except sqlite3.Error as e:
            logger.error(f"Error getting user ID: {e}")
            return None
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
            logger.info("✅ Database connection closed")

# Test function
def test_database():
    """Test the database functionality"""
    print("🚀 Testing database setup...")
    db = Database()
    
    # Test creating a user
    test_user = {
        'telegram_id': 123456789,
        'username': 'testuser',
        'nickname': 'TestUser',
        'full_name': 'Test User',
        'sex': 'male',
        'age_group': '25-30'
    }
    
    if db.create_user(test_user):
        print("✅ User created successfully")
        
        # Test getting the user
        user = db.get_user(123456789)
        if user:
            print(f"✅ User retrieved: {user['nickname']} with {user['points']} points")
    
    db.close()
    print("✅ Database test completed!")

if __name__ == "__main__":
    test_database()