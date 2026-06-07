
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from app.database.connection import SessionLocal
from app.models.user import User
from passlib.hash import bcrypt

if len(sys.argv) != 3:
    print("Usage: python pswd-reset.py <user_id> <new_password>")
    sys.exit(1)

user_id = int(sys.argv[1])
new_password = sys.argv[2]

db = SessionLocal()
user = db.query(User).filter(User.id == user_id).first()

if not user:
    print("User not found")
    sys.exit(1)

user.password = bcrypt.hash(new_password)
db.commit()
print(f"✅ Password reset for user {user.username} (id={user.id})")
