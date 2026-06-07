from passlib.hash import bcrypt

new_password = "@Capri001"
hashed_pw = bcrypt.hash(new_password)
print(hashed_pw)  # This is what you'll store in DB
