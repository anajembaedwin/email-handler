import os
import time
import json
import imaplib
import email
import re
import html
import redis
import logging
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler

# Load environment variables from .env file
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Redis client
REDIS_HOST = os.getenv('REDIS_HOST')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))  # Default Redis port
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', None)

# Check essential environment variables
required_vars = ['REDIS_HOST', 'EMAIL_USER', 'EMAIL_PASSWORD', 'IMAP_SERVER', 'IMAP_PORT']
missing_vars = [var for var in required_vars if not os.getenv(var)]
if missing_vars:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")

redis_client = redis.StrictRedis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    decode_responses=True
)

def connect_to_imap():
    """Connect to the IMAP server using credentials from environment variables."""
    email_user = os.getenv('EMAIL_USER')
    email_password = os.getenv('EMAIL_PASSWORD')
    imap_server = os.getenv('IMAP_SERVER')
    imap_port = int(os.getenv('IMAP_PORT', 993))  # Default IMAP SSL port

    try:
        logger.info(f"Attempting to log into IMAP server for {email_user}")
        session = imaplib.IMAP4_SSL(imap_server, imap_port)
        session.login(email_user, email_password)
        logger.info("Logged into IMAP server successfully.")
        return session
    except imaplib.IMAP4.error as e:
        logger.error(f"Failed to login to IMAP server: {e}")
        return None

def fetch_emails():
    """Fetch unread verification and activation emails, store details in Redis."""
    retries = 3  # Number of retry attempts
    retry_delay = 5  # Delay between retries in seconds

    for attempt in range(retries):
        imap_session = connect_to_imap()
        if imap_session is None:
            logger.error("IMAP session could not be established.")
            continue

        try:
            imap_session.select('inbox')
            logger.info("Searching for unread verification and activation emails...")
            result, data = imap_session.search(None, '(UNSEEN (OR (SUBJECT "verification code") (BODY "Activate Your Account")))')
            email_ids = data[0].split()

            if not email_ids:
                logger.info("No new emails found matching the search criteria.")
                return

            logger.info(f"Found {len(email_ids)} unread email(s) matching the criteria.")
            email_list = []

            for email_id in email_ids:
                result, data = imap_session.fetch(email_id, '(RFC822)')
                msg = email.message_from_bytes(data[0][1])

                # Decode subject and parse date
                email_subject = email.header.decode_header(msg['Subject'])[0][0]
                if isinstance(email_subject, bytes):
                    email_subject = email_subject.decode()
                email_date = msg['Date']
                parsed_date = email.utils.parsedate_to_datetime(email_date)
                email_list.append((parsed_date, email_subject, email_id, msg))

            email_list.sort(key=lambda x: x[0], reverse=False)

            # Process emails based on content
            for parsed_date, email_subject, email_id, msg in email_list:
                email_to = msg['To'].lower()
                if 'verification code' in email_subject.lower():
                    verification_code = email_subject.split()[0]
                    redis_client.set(f'{email_to}-verify', verification_code, ex=172800)
                    logger.info(f"Verify: {verification_code} - {email_to}")
                elif 'activate your account' in email_subject.lower():
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            body = part.get_payload(decode=True).decode()
                            match = re.search(r'href="(https://seller-us-accounts.tiktok.com/profile/activate-page[^"]+)"', body, re.IGNORECASE)
                            if match:
                                activation_link = html.unescape(match.group(1))
                                redis_client.set(f'{email_to}-activate', activation_link, ex=172800)
                                logger.info(f"Stored activation link for {email_to}")

            # Mark emails as seen
            # Note: Comment out the next two lines during testing to avoid marking emails as read.
            # for email_id in email_ids:
            #     imap_session.store(email_id, '+FLAGS', '\\Seen')
            # logger.info("All matched emails processed and marked as seen.")
            break  # Exit loop if successful

        except Exception as e:
            logger.error(f"Error fetching emails: {e}")
            if attempt < retries - 1:
                logger.info(f"Retrying after {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error("Failed after multiple attempts")
        finally:
            if imap_session:
                imap_session.logout()
                logger.info("IMAP session closed after fetching emails.")

def clean_folders():
    """Clean specified email folders once a day at midnight."""
    imap_session = connect_to_imap()
    if imap_session is None:
        logger.error("IMAP session could not be established.")
        return

    folders = ['INBOX', '[Gmail]/Spam', '[Gmail]/Trash']
    try:
        for folder in folders:
            imap_session.select(folder)
            logger.info(f"Cleaning folder: {folder}")
            typ, data = imap_session.search(None, 'ALL')
            mail_ids = data[0].split()

            if not mail_ids:
                logger.info(f"No emails found in folder {folder} to clean.")
                continue

            batch_size = 100
            logger.info(f"Found {len(mail_ids)} email(s) in folder {folder} to delete.")
            for i in range(0, len(mail_ids), batch_size):
                batch = mail_ids[i:i + batch_size]
                for mail_id in batch:
                    imap_session.store(mail_id, '+FLAGS', '\\Deleted')
                imap_session.expunge()
                logger.info(f"Deleted {len(batch)} emails from folder {folder}")

        logger.info("All specified folders have been cleaned.")
    except Exception as e:
        logger.error(f"Error cleaning folders: {e}")
    finally:
        imap_session.logout()
        logger.info("IMAP session closed after cleaning folders.")

@app.route('/retrieveEmailCode', methods=['GET'])
def retrieve_email_code():
    email_to = request.args.get('email')
    if not email_to:
        logger.warning("Email parameter missing in request to /retrieveEmailCode")
        return {'error': 'Email parameter is required'}, 400

    if email_to.endswith('-verify'):
        verify_code = redis_client.get(email_to)
        if not verify_code:
            logger.info(f"Verification code not found for {email_to}")
            return {'error': 'Verification code not found'}, 404
        logger.info(f"Fetched verification code for {email_to}")
        return {'verification_code': verify_code}, 200
    elif email_to.endswith('-activate'):
        activate_code = redis_client.get(email_to)
        if not activate_code:
            logger.info(f"Activation link not found for {email_to}")
            return {'error': 'Activation link not found'}, 404
        logger.info(f"Fetched activation link for {email_to}")
        return {'activation_link': activate_code}, 200
    else:
        logger.info(f"Invalid email address format for {email_to}")
        return {'error': 'Invalid email address format'}, 400

# Schedule periodic tasks
scheduler = BackgroundScheduler()
scheduler.add_job(fetch_emails, 'interval', seconds=30)  # Fetch emails every 30 seconds
# scheduler.add_job(clean_folders, 'cron', hour=0, minute=0)  # Clean folders daily at midnight
scheduler.start()
logger.info("Scheduler started for periodic tasks.")

if __name__ == "__main__":
    logger.info("Starting Flask app...")
    app.run(host='127.0.0.1', port=5000)
