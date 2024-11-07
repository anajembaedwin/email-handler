import os
import json
import imaplib
import email
import re
import html
import ssl
import redis
import time
from dotenv import load_dotenv
from flask import Flask, request

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Initialize global variables
imap_session = None

# Initialize Redis client
REDIS_HOST = os.getenv('REDIS_HOST')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))  # Default Redis port
# REDIS_PASSWORD = os.getenv('REDIS_PASSWORD')

redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

# redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)

def connect_to_imap():
    global imap_session
    
    if imap_session is None:
        email_user = os.getenv('EMAIL_USER')
        email_password = os.getenv('EMAIL_PASSWORD')
        imap_server = os.getenv('IMAP_SERVER')
        imap_port = int(os.getenv('IMAP_PORT', 993))  # Default IMAP SSL port

        if not all([email_user, email_password, imap_server, imap_port]):
            raise ValueError("Please ensure that EMAIL_USER, EMAIL_PASSWORD, IMAP_SERVER, and IMAP_PORT are set in environment variables.")

        print(f"Logging into IMAP server for {email_user}")
        imap_session = imaplib.IMAP4_SSL(imap_server, imap_port)
        imap_session.login(email_user, email_password)
    return imap_session

def fetch_emails():
    global imap_session
    try:
        connect_to_imap()  # Ensure we have a connected session
        imap_session.select('inbox')

        # Search for unread emails
        print("Searching for unread verification and activation emails...")
        result, data = imap_session.search(None, '(UNSEEN (OR (SUBJECT "verification code") (BODY "Activate Your Account")))')
        email_ids = data[0].split()

        email_list = []

        for email_id in email_ids:
            result, data = imap_session.fetch(email_id, '(RFC822)')
            msg = email.message_from_bytes(data[0][1])

            email_subject = email.header.decode_header(msg['Subject'])[0][0]
            if isinstance(email_subject, bytes):
                email_subject = email_subject.decode()

            # Extract the date and time
            email_date = msg['Date']
            parsed_date = email.utils.parsedate_to_datetime(email_date)

            # Add email details to the list
            email_list.append((parsed_date, email_subject, email_id, msg))

        # Sort the list by datetime (most recent first)
        email_list.sort(key=lambda x: x[0], reverse=False)

        # Process sorted emails
        for parsed_date, email_subject, email_id, msg in email_list:
            # Check for verification code in subject
            if 'verification code' in email_subject.lower():
                print(f"Found verification email: {email_subject}")
                verification_code = email_subject.split()[0]  # Logic to extract verification code
                # Store in Redis with the key format: email_to-verify
                email_to = msg['To'].lower()
                redis_client.set(f'{email_to}-verify', verification_code, ex=172800)

                # Print formatted verification info
                print(f"Verify: {verification_code} - {email_to}")

            # Check for activation link in the body
            elif 'activate your account' in email_subject.lower():
                print(f"Found activation email: {email_subject}")
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        body = part.get_payload(decode=True).decode()
                        match = re.search(r'href="(https://seller-us-accounts.tiktok.com/profile/activate-page[^"]+)"', body, re.IGNORECASE)
                        if match:
                            activation_link = html.unescape(match.group(1))
                            # Store in Redis with the key format: email_to-activate
                            email_to = msg['To'].lower()
                            redis_client.set(f'{email_to}-activate', activation_link, ex=172800)

            # Mark email as seen
            # imap_session.store(email_id, '+FLAGS', '\\Seen')

    except Exception as e:
        print(f"An error occurred: {e}")
        imap_session = None  # Invalidate session on error

def clean_folders():
    global imap_session
    try:
        connect_to_imap()  # Ensure we have a connected session
        # Define folders to clean
        folders = ['INBOX', '[Gmail]/Spam', '[Gmail]/Trash']  # Adjust folder names for provider

        # Loop through each folder and delete all emails
        for folder in folders:
            # Select the folder
            imap_session.select(folder)
            print(f"Cleaning folder: {folder}")

            # Search for all emails
            typ, data = imap_session.search(None, 'ALL')
            mail_ids = data[0]

            # If there are emails, proceed with deletion
            if mail_ids:
                mail_ids_list = mail_ids.split()

                # Batch size for expunging emails in chunks
                batch_size = 100
                for i in range(0, len(mail_ids_list), batch_size):
                    batch = mail_ids_list[i:i + batch_size]
                    
                    # Mark emails for deletion in bulk
                    for mail_id in batch:
                        imap_session.store(mail_id, '+FLAGS', '\\Deleted')
                    
                    # Periodically expunge to apply the deletion for each batch
                    imap_session.expunge()
                    print(f"Expunged {len(batch)} emails from folder: {folder}")

        print("All specified folders have been cleaned.")
    except Exception as e:
        print(f"An error occurred: {str(e)}")
    finally:
        imap_session.logout()

@app.route('/getEmailCodes', methods=['GET'])
def get_email_codes():
    email_to = request.args.get('email')
    if not email_to:
        return {'error': 'Email parameter is required'}, 400

    verify_code = redis_client.get(f'{email_to}-verify')
    activate_code = redis_client.get(f'{email_to}-activate')

    if not verify_code or not activate_code:
        return {'error': 'Verification code not found'}, 404

    return {
        'verification_code': verify_code,
        'activation_link': activate_code
    }, 200


def daily_cleanup():
    while True:
        now = time.localtime()
        if now.tm_hour == 0 and now.tm_min == 0:  # Check if it's midnight
            clean_folders()
            time.sleep(60)  # Wait for a minute to avoid multiple executions
        time.sleep(30)  # Poll every 30 seconds

def instance_handler(event, context):
    fetch_emails()

    return {
        'statusCode': 200,
        'body': 'Email fetching complete!'
    }

def main():
    # Call instance_handler every 30 seconds
    def instance_handler_loop():
        while True:
            instance_handler(None, None)
            time.sleep(5)

    # Call daily_cleanup once a day at midnight
    # def daily_cleanup_loop():
    #     while True:
    #         daily_cleanup()
    #         time.sleep(86400)  # 1 day

    # Start the loops
    import threading
    threading.Thread(target=instance_handler_loop).start()
    # threading.Thread(target=daily_cleanup_loop).start()

    # Start the Flask app
    app.run(host='0.0.0.0', port=5000)

if __name__ == "__main__":
    main()
