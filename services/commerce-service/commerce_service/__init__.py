"""commerce-service (R5e step 7)."""
import os

# Set here, before any module of the service is imported: the internal client
# presents this service's own certificate (internal TLS), and the database layer
# picks commerce's own pool from it (VELARIS_CASE_COMMERCE_DATABASE_POOLER_URL).
os.environ.setdefault("VELARIS_SERVICE_NAME", "commerce-service")
