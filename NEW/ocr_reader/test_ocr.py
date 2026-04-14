"""
Test OCR reading - see what it extracts from your screen
Run this to test OCR accuracy before using in dashboard
"""
import sys
import os
from PIL import Image
import pytesseract
import mss

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

def test_ocr():
    """Test OCR on current screen"""
    print("=" * 60)
    print("🧪 Testing OCR Reading")
    print("=" * 60)
    print("\n1. Make sure BookieBeats is open and visible")
    print("2. This will take a screenshot in 3 seconds...")
    print("3. Press Ctrl+C to cancel\n")
    
    import time
    for i in range(3, 0, -1):
        print(f"   {i}...")
        time.sleep(1)
    
    # Capture screen
    print("\n📸 Taking screenshot...")
    sct = mss.mss()
    
    # Full screen or specific region
    print("   Options:")
    print("   1. Full screen")
    print("   2. Specific region (you'll enter coordinates)")
    choice = input("   Choice (1 or 2): ").strip()
    
    if choice == "2":
        left = int(input("   Left (X): ") or "0")
        top = int(input("   Top (Y): ") or "0")
        width = int(input("   Width: ") or "1920")
        height = int(input("   Height: ") or "1080")
        region = {'left': left, 'top': top, 'width': width, 'height': height}
    else:
        # Get screen size
        monitor = sct.monitors[1]  # Primary monitor
        region = {
            'left': monitor['left'],
            'top': monitor['top'],
            'width': monitor['width'],
            'height': monitor['height']
        }
    
    screenshot = sct.grab(region)
    img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
    
    # Save screenshot for inspection
    screenshot_path = "ocr_test_screenshot.png"
    img.save(screenshot_path)
    print(f"   ✅ Screenshot saved: {screenshot_path}")
    
    # Run OCR
    print("\n🔍 Running OCR...")
    try:
        text = pytesseract.image_to_string(img, config='--psm 6')
        print("\n" + "=" * 60)
        print("📝 EXTRACTED TEXT:")
        print("=" * 60)
        print(text)
        print("=" * 60)
        
        # Save text to file
        with open("ocr_test_output.txt", "w", encoding="utf-8") as f:
            f.write(text)
        print("\n✅ Text saved to: ocr_test_output.txt")
        
        # Try to find alerts
        print("\n🔍 Looking for alerts...")
        import re
        
        # Look for team patterns
        teams = re.findall(r'([A-Za-z\s]+)\s*@\s*([A-Za-z\s]+)', text)
        if teams:
            print(f"   Found {len(teams)} team match(es):")
            for away, home in teams[:5]:  # Show first 5
                print(f"      {away.strip()} @ {home.strip()}")
        
        # Look for EV percentages
        evs = re.findall(r'(\d+\.?\d*)\s*%', text)
        if evs:
            print(f"   Found {len(evs)} EV percentage(s):")
            for ev in evs[:10]:  # Show first 10
                print(f"      {ev}%")
        
        # Look for odds
        odds = re.findall(r'([+-]\d+)', text)
        if odds:
            print(f"   Found {len(odds)} odds value(s):")
            for odd in odds[:10]:  # Show first 10
                print(f"      {odd}")
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
        print("   💡 Make sure Tesseract is installed and in PATH")
    
    print("\n" + "=" * 60)
    print("✅ Test complete!")
    print("=" * 60)
    print("\nIf OCR looks good, you can use OCR Reader in dashboard.")
    print("If not, try:")
    print("  - Adjusting screen region")
    print("  - Making BookieBeats window larger")
    print("  - Improving image contrast")

if __name__ == "__main__":
    try:
        test_ocr()
    except KeyboardInterrupt:
        print("\n\n❌ Cancelled")
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
