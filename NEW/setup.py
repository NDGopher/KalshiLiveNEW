"""
Setup script for Browser Reader Dashboard
Run this once to set up everything
"""
import os
import sys
import subprocess

def main():
    print("=" * 60)
    print("🚀 Browser Reader Dashboard Setup")
    print("=" * 60)
    
    # Check Python version
    if sys.version_info < (3, 8):
        print("❌ Python 3.8+ required")
        sys.exit(1)
    
    print("✅ Python version OK")
    
    # Install requirements
    print("\n📦 Installing requirements...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        print("✅ Requirements installed")
    except subprocess.CalledProcessError:
        print("❌ Failed to install requirements")
        sys.exit(1)
    
    # Install Playwright browsers
    print("\n🌐 Installing Playwright browsers...")
    try:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        print("✅ Playwright browsers installed")
    except subprocess.CalledProcessError:
        print("❌ Failed to install Playwright browsers")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("✅ Setup complete!")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Start Chrome with: chrome.exe --remote-debugging-port=9222")
    print("2. Open BookieBeats and log in")
    print("3. Run: python dashboard_browser.py")
    print("\nSee START_HERE.md for detailed instructions")

if __name__ == "__main__":
    main()
