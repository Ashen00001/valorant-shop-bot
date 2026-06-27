#!/usr/bin/env python3
"""
Add (or update) a Valorant account for a Discord user.
Run this on your LOCAL machine (not the VPS) — it opens a browser to log in.

Usage:
  python setup_account.py <discord_user_id> [region]

Example:
  python setup_account.py 123456789012345678 na

Regions: na eu ap kr br latam
"""
import sys, webbrowser
import riot_auth


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    discord_id = sys.argv[1]
    region     = (sys.argv[2] if len(sys.argv) > 2 else "na").lower()

    print(f"Discord user : {discord_id}")
    print(f"Region       : {region}")
    print()

    auth_url, verifier = riot_auth.get_browser_login_url()

    print("Step 1 — A browser will open to the Riot login page.")
    print("         Log in with your Riot account (username + password, or social login).")
    print()
    print("Step 2 — After logging in, your browser will show an error page.")
    print("         That's normal. Copy the FULL URL from the address bar.")
    print("         It will start with: http://localhost/redirect?code=...")
    print()

    opened = False
    try:
        webbrowser.open(auth_url)
        opened = True
    except Exception:
        pass

    if not opened:
        print("Could not open browser automatically. Open this URL manually:")
        print(f"\n  {auth_url}\n")
    else:
        print("Browser opened. Waiting for you to log in...")
        print()

    redirect_url = input("Paste the redirect URL here: ").strip()

    print("\nLogging in...", end="", flush=True)
    try:
        account = riot_auth.complete_browser_login(redirect_url, verifier, region)
        print(" ✅")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)

    print(f"PUUID: {account['puuid']}")

    accounts = riot_auth.load_accounts()
    existing = accounts.get(discord_id, [])
    if not isinstance(existing, list):     # migrate old single-account format
        existing = [existing]
    existing = [a for a in existing if a.get("puuid") != account["puuid"]]  # re-link replaces, no dupes
    existing.append(account)
    accounts[discord_id] = existing
    riot_auth.save_accounts(accounts)

    print(f"\n✅ Saved to accounts.json ({len(existing)} account(s) for this user)")
    print()
    print("Next: copy accounts.json to the VPS:")
    print(f'  scp -i "%USERPROFILE%\\.ssh\\shopbot" accounts.json ubuntu@<your-vps-ip>:/home/ubuntu/valorant-shop-bot/accounts.json')


if __name__ == "__main__":
    main()
