import bcrypt

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain password against a hashed password using bcrypt.

    Args:
        plain_password (str): The plain text password to verify.
        hashed_password (str): The hashed password stored in the database.

    Returns:
        bool: True if the password matches, False otherwise.
    """
    # Encode the passwords to bytes as bcrypt requires byte inputs
    plain_password_bytes = plain_password.encode('utf-8')
    hashed_password_bytes = hashed_password.encode('utf-8')
    
    # Use bcrypt to check if the plain password matches the hashed password
    return bcrypt.checkpw(plain_password_bytes, hashed_password_bytes)