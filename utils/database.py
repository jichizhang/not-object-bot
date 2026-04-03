import sqlite3


def init_database():
    """Initialize the database with the users table"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            coins INTEGER DEFAULT 0,
            lifetime_coins INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_checkins (
            user_id INTEGER PRIMARY KEY,
            last_checkin_date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_messages (
            user_id INTEGER PRIMARY KEY,
            last_message_date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS custom_roles (
            user_id INTEGER PRIMARY KEY,
            role_id INTEGER NOT NULL,
            role_name TEXT NOT NULL,
            color INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sotd_songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            track_name TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            album_cover_url TEXT NOT NULL,
            spotify_url TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            date_added TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS snap_streaks (
            user_id INTEGER PRIMARY KEY,
            last_snap_date TEXT NOT NULL,
            streak_days INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS birthdays (
            user_id INTEGER PRIMARY KEY,
            month INTEGER NOT NULL,
            day INTEGER NOT NULL,
            year INTEGER,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            removed INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    
    # Add lifetime_coins column if it doesn't exist (for existing databases)
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN lifetime_coins INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        # Column already exists, ignore
        pass
    
    # Update existing users to have lifetime_coins equal to their current coins
    cursor.execute('UPDATE users SET lifetime_coins = coins WHERE lifetime_coins = 0 OR lifetime_coins IS NULL')
    
    conn.commit()
    conn.close()


def get_user_coins(user_id):
    """Get the coin balance for a specific user, creating them with 1000 coins if they don't exist"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Check if user exists
    cursor.execute('SELECT coins FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    if result:
        # User exists, return their coins
        conn.close()
        return result[0]
    else:
        # User doesn't exist, create them with 1000 coins
        cursor.execute('INSERT INTO users (user_id, username, coins, lifetime_coins) VALUES (?, ?, ?, ?)', 
                      (user_id, "Unknown", 1000, 1000))
        conn.commit()
        conn.close()
        return 1000


def get_user_lifetime_coins(user_id):
    """Get the lifetime coin balance for a specific user, creating them with 1000 coins if they don't exist"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Check if user exists
    cursor.execute('SELECT lifetime_coins FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    if result:
        # User exists, return their lifetime coins
        conn.close()
        return result[0]
    else:
        # User doesn't exist, create them with 1000 coins
        cursor.execute('INSERT INTO users (user_id, username, coins, lifetime_coins) VALUES (?, ?, ?, ?)', 
                      (user_id, "Unknown", 1000, 1000))
        conn.commit()
        conn.close()
        return 1000


def add_coins(user_id, username, amount):
    """Add coins to a user's balance, giving new users 1000 coins base"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, username, coins, lifetime_coins)
        VALUES (?, ?, 
                COALESCE((SELECT coins FROM users WHERE user_id = ?), 1000) + ?,
                COALESCE((SELECT lifetime_coins FROM users WHERE user_id = ?), 1000) + ?)
    ''', (user_id, username, user_id, amount, user_id, amount))
    conn.commit()
    conn.close()


def remove_coins(user_id, username, amount):
    """Remove coins from a user's balance (minimum 0), giving new users 1000 coins base"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, username, coins, lifetime_coins)
        VALUES (?, ?, 
                MAX(COALESCE((SELECT coins FROM users WHERE user_id = ?), 1000) - ?, 0),
                MAX(COALESCE((SELECT lifetime_coins FROM users WHERE user_id = ?), 1000) - ?, 0))
    ''', (user_id, username, user_id, amount, user_id, amount))
    conn.commit()
    conn.close()


def spend_coins(user_id, username, amount):
    """Spend coins from a user's balance. Returns True if successful, False if insufficient funds"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Check current balance (this will create user with 1000 coins if they don't exist)
    current_coins = get_user_coins(user_id)
    if current_coins < amount:
        conn.close()
        return False
    
    # Deduct coins (lifetime_coins remains unchanged when spending)
    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, username, coins, lifetime_coins)
        VALUES (?, ?, ?, COALESCE((SELECT lifetime_coins FROM users WHERE user_id = ?), 1000))
    ''', (user_id, username, current_coins - amount, user_id))
    
    conn.commit()
    conn.close()
    return True


def get_leaderboard(limit=10):
    """Get the top users by lifetime coin balance"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    cursor.execute('SELECT username, coins, lifetime_coins FROM users ORDER BY lifetime_coins DESC LIMIT ?', (limit,))
    results = cursor.fetchall()
    conn.close()
    return results


def can_daily_checkin(user_id):
    """Check if a user can perform a daily check-in (based on UTC date)"""
    from datetime import datetime, timezone
    
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Get the last check-in date
    cursor.execute('SELECT last_checkin_date FROM daily_checkins WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    conn.close()
    
    if not result:
        return True  # User has never checked in
    
    last_checkin = result[0]
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    return last_checkin != today_utc


def perform_daily_checkin(user_id, username, coin_amount=200):
    """Perform a daily check-in for a user and return the new coin balance"""
    from datetime import datetime, timezone
    
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Add coins
    add_coins(user_id, username, coin_amount)
    
    # Update check-in date
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cursor.execute('''
        INSERT OR REPLACE INTO daily_checkins (user_id, last_checkin_date)
        VALUES (?, ?)
    ''', (user_id, today_utc))
    
    conn.commit()
    conn.close()
    
    # Return the new coin balance
    return get_user_coins(user_id)


def can_earn_daily_message_reward(user_id):
    """Check if a user can earn coins for their first message of the day (based on UTC date)"""
    from datetime import datetime, timezone
    
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Get the last message date
    cursor.execute('SELECT last_message_date FROM daily_messages WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    conn.close()
    
    if not result:
        return True  # User has never sent a message
    
    last_message = result[0]
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    return last_message != today_utc


def process_daily_message_reward(user_id, username, coin_amount=200):
    """Process daily message reward for a user and return the new coin balance"""
    from datetime import datetime, timezone
    
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Add coins
    add_coins(user_id, username, coin_amount)
    
    # Update message date
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cursor.execute('''
        INSERT OR REPLACE INTO daily_messages (user_id, last_message_date)
        VALUES (?, ?)
    ''', (user_id, today_utc))
    
    conn.commit()
    conn.close()
    
    # Return the new coin balance
    return get_user_coins(user_id)


def get_user_custom_role(user_id):
    """Get a user's custom role information"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    cursor.execute('SELECT role_id, role_name, color FROM custom_roles WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result


def create_user_custom_role(user_id, role_id, role_name, color):
    """Create or update a user's custom role"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO custom_roles (user_id, role_id, role_name, color)
        VALUES (?, ?, ?, ?)
    ''', (user_id, role_id, role_name, color))
    conn.commit()
    conn.close()


def delete_user_custom_role(user_id):
    """Delete a user's custom role from the database"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM custom_roles WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()


def refund_coins(user_id, username, amount):
    """Refund coins to a user's current balance without affecting lifetime coins"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, username, coins, lifetime_coins)
        VALUES (?, ?, 
                COALESCE((SELECT coins FROM users WHERE user_id = ?), 1000) + ?,
                COALESCE((SELECT lifetime_coins FROM users WHERE user_id = ?), 1000))
    ''', (user_id, username, user_id, amount, user_id))
    conn.commit()
    conn.close()


def add_sotd_song(user_id, track_name, artist_name, album_cover_url, spotify_url):
    """Add a song to the SOTD database"""
    from datetime import datetime, timezone
    
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cursor.execute('''
        INSERT INTO sotd_songs (user_id, track_name, artist_name, album_cover_url, spotify_url, date_added)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, track_name, artist_name, album_cover_url, spotify_url, today_utc))
    
    conn.commit()
    conn.close()


def get_random_unused_song():
    """Get a random unused song from the database"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # First, get all distinct users who have unused songs
    cursor.execute('''
        SELECT DISTINCT user_id
        FROM sotd_songs
        WHERE used = 0
        ORDER BY RANDOM()
        LIMIT 1
    ''')
    user_result = cursor.fetchone()
    
    if not user_result:
        conn.close()
        return None
    
    selected_user_id = user_result[0]
    
    # Then, get a random unused song from that user
    cursor.execute('''
        SELECT id, user_id, track_name, artist_name, album_cover_url, spotify_url
        FROM sotd_songs
        WHERE used = 0 AND user_id = ?
        ORDER BY RANDOM()
        LIMIT 1
    ''', (selected_user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return {
            'id': result[0],
            'user_id': result[1],
            'track_name': result[2],
            'artist_name': result[3],
            'album_cover_url': result[4],
            'spotify_url': result[5]
        }
    return None


def mark_song_as_used(song_id):
    """Mark a song as used"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE sotd_songs
        SET used = 1
        WHERE id = ?
    ''', (song_id,))
    
    conn.commit()
    conn.close()


def remove_pending_songs(user_id):
    """Remove all unused (pending) songs for a user"""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM sotd_songs WHERE user_id = ? AND used = 0', (user_id,))
    conn.commit()
    conn.close()


def can_add_song(track_name, artist_name):
    """Check if a song can be added to the database.
    Returns True if:
    - Song doesn't exist, OR
    - Song exists but all entries have been featured
    Returns False if:
    - Song already has an unused entry (waiting to be featured)
    """
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Get all entries for this track and artist combination
    cursor.execute('SELECT used FROM sotd_songs WHERE track_name = ? AND artist_name = ?', (track_name, artist_name))
    results = cursor.fetchall()
    conn.close()
    
    # If no entries exist, allow it
    if not results:
        return True, None
    
    # Check if there's at least one unused entry (waiting to be featured)
    has_unused = any(result[0] == 0 for result in results)
    
    if has_unused:
        # Song is already in the queue waiting to be featured
        return False, "unused"
    
    # All entries have been featured, allow adding it again
    return True, "all_featured"


def can_snap_today(user_id):
    """Check if a user can snap today (based on UTC date). Returns True/False and streak info"""
    from datetime import datetime, timezone
    
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Get the last snap date and current streak
    cursor.execute('SELECT last_snap_date, streak_days FROM snap_streaks WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    conn.close()
    
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    if not result:
        # User has never snapped before
        return True, 0, 0
    
    last_snap_date, current_streak = result
    
    # Get yesterday's date
    from datetime import timedelta
    yesterday_utc = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
    
    if last_snap_date == today_utc:
        # User already snapped today
        return False, current_streak, 0
    
    if last_snap_date == yesterday_utc:
        # User snapped yesterday, continue streak
        return True, current_streak + 1, current_streak + 1
    
    # Streak broken, reset to 0
    return True, 0, 0


def process_snap(user_id, username):
    """Process a snap and return the reward amount, new streak, and new balance"""
    from datetime import datetime, timezone
    
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    # Get current streak info
    can_snap, new_streak_days, _ = can_snap_today(user_id)
    
    # Calculate reward: Day 1 = 25, Day 2 = 50, Day 3 = 75, ... capped at 500
    # For streak_days = 0 (first snap), reward = 25
    # For streak_days = n (nth day of streak), reward = min(25 * (n+1), 500)
    reward = min(25 * (new_streak_days + 1), 500)
    
    # Add coins
    add_coins(user_id, username, reward)
    
    # Update snap streak info
    cursor.execute('''
        INSERT OR REPLACE INTO snap_streaks (user_id, last_snap_date, streak_days)
        VALUES (?, ?, ?)
    ''', (user_id, today_utc, new_streak_days))
    
    conn.commit()
    conn.close()
    
    # Return reward amount, streak days, and new balance
    return reward, new_streak_days, get_user_coins(user_id)


def set_user_birthday(user_id, month, day, year=None, timezone='UTC'):
    """Set or update a user's birthday. Returns True if this is the first time setting it."""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    # Check if user already has a birthday set
    cursor.execute('SELECT removed FROM birthdays WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    is_first_time = result is None
    
    # Insert or update birthday
    cursor.execute('''
        INSERT OR REPLACE INTO birthdays (user_id, month, day, year, timezone, removed)
        VALUES (?, ?, ?, ?, ?, 0)
    ''', (user_id, month, day, year, timezone))
    
    conn.commit()
    conn.close()
    
    return is_first_time


def get_user_birthday(user_id):
    """Get a user's birthday. Returns None if not set or removed."""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT month, day, year, timezone FROM birthdays WHERE user_id = ? AND removed = 0', (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return {
            'month': result[0],
            'day': result[1],
            'year': result[2],
            'timezone': result[3]
        }
    return None


def get_all_active_birthdays():
    """Get all active (non-removed) birthdays."""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT user_id, month, day, year, timezone FROM birthdays WHERE removed = 0')
    results = cursor.fetchall()
    conn.close()
    
    birthdays = []
    for result in results:
        birthdays.append({
            'user_id': result[0],
            'month': result[1],
            'day': result[2],
            'year': result[3],
            'timezone': result[4]
        })
    return birthdays


def get_unique_timezones():
    """Get all unique timezones from active birthdays."""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT DISTINCT timezone FROM birthdays WHERE removed = 0')
    results = cursor.fetchall()
    conn.close()
    
    return [result[0] for result in results]


def remove_user_birthday(user_id):
    """Mark a user's birthday as removed (don't delete it)."""
    conn = sqlite3.connect('not_object.db')
    cursor = conn.cursor()
    
    cursor.execute('UPDATE birthdays SET removed = 1 WHERE user_id = ?', (user_id,))
    
    conn.commit()
    conn.close()
