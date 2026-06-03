"""Import all connector implementations so they self-register."""
from . import http_connector      # noqa: F401
from . import webhook_connector   # noqa: F401
from . import stripe_connector    # noqa: F401  — P48 payment
from . import onfido_connector      # noqa: F401  — P49 KYC
from . import docusign_connector    # noqa: F401  — P49 e-sign
from . import salesforce_connector  # noqa: F401  — P50 CRM
from . import xero_connector        # noqa: F401  — P50 accounting
from . import twilio_connector      # noqa: F401  — P51 SMS
from . import slack_connector       # noqa: F401  — P51 Slack
from . import docling_connector     # noqa: F401  — P52 doc extraction
from . import s3_connector          # noqa: F401  — P52 cloud storage
from . import http_custom_connector # noqa: F401  — P53 custom HTTP
