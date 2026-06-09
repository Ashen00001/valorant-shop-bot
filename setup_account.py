#!/usr/bin/env python3
"""
Add (or update) a Valorant account for a Discord user.
Run this once per user on the machine running the bot.

Usage:
  python setup_account.py <discord_user_id> [region]

Example:
  python setup_account.py 123456789012345678 na

Regions: na eu ap kr br latam
"""
import sys, getpass
import riot_auth


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    discord_id = sys.argv[1]
    region     = (sys.argv[2] if len(sys.argv) > 2 else "na").lower()

    print(f"Discord user : {discord_id}")
    print(f"Region       : {region}")
    print("Password is used once to get auth cookies — it is NEVER stored.\n")

    username = input("Riot username (email or gamename#tagline): ").strip()
    password = getpass.getpass("Password: ")

    print("\nLogging in...", end="", flush=True)
    try:
        account = riot_auth.login(username, password, region)
        print(" ✅")
    except ValueError as e:
        if "MFA_REQUIRED" in str(e):
            print("\n2FA required.")
            code    = input("Authenticator code: ").strip()
            account = riot_auth.login_mfa(username, code)
        else:
            print(f"\n❌ Login failed: {e}")
            sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)

    print(f"PUUID: {account['puuid']}")

    accounts = riot_auth.load_accounts()
    accounts[discord_id] = account
    riot_auth.save_accounts(accounts)

    print(f"\n✅ Saved to accounts.json — restart the bot to apply.")
    print("Note: cookies expire after ~30 days. Re-run this script to refresh.")


if __name__ == "__main__":
    main()
