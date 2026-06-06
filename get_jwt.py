import sys, uuid
from datetime import datetime, timedelta, timezone
from jose import jwt

SECRET_KEY = "lezUBLzrNkRda0fLG/9VRxQEsZYGJR6B/Z9YWz0xPyD9JgYdzlFIQxe4XJtFWHAgvhNnxAenzhaS2gTehVxmiw=="
now = datetime.now(timezone.utc)
payload = {
    'sub': '1',
    'exp': now + timedelta(hours=2),
    'jti': str(uuid.uuid4()),
}
token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
print(token)
