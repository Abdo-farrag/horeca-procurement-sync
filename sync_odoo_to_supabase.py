import os
from dotenv import load_dotenv

# Load local .env if it exists
load_dotenv()

def main():
    print("Starting Odoo to Supabase Sync...")
    
    # Check for required environment variables
    required_vars = [
        "ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_API_KEY",
        "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "DAYS_BACK"
    ]
    
    missing = []
    for var in required_vars:
        val = os.getenv(var)
        if not val:
            missing.append(var)
        else:
            # Masking value for safety even in logs
            print(f"Found {var}")

    if missing:
        print(f"Error: Missing environment variables: {', '.join(missing)}")
    else:
        print("All required environment variables are present.")
        print("Sync process completed successfully (Placeholder).")

if __name__ == "__main__":
    main()
