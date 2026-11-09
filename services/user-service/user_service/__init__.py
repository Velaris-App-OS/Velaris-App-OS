"""user-service — sign-in and identity (R5e step 10)."""
import os

# Set here, before any module of the service is imported: the internal client
# presents this service's own certificate (internal TLS), and the database layer
# picks its own pool from it (VELARIS_CASE_USER_DATABASE_POOLER_URL).
os.environ.setdefault("VELARIS_SERVICE_NAME", "user-service")
