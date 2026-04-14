"""
Helper script to display your PC's IP address for remote access
Run this to see what IP address to use from your phone
"""
import socket
import sys

def get_local_ip():
    """Get the local IP address of this machine"""
    try:
        # Connect to a remote address (doesn't actually connect, just gets local IP)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None

def get_all_ips():
    """Get all IP addresses of this machine"""
    ips = []
    hostname = socket.gethostname()
    
    # Get all IPs associated with hostname
    try:
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ip and ip != '127.0.0.1' and not ip.startswith('::'):
                if ip not in ips:
                    ips.append(ip)
    except:
        pass
    
    # Also try the method that works on most systems
    local_ip = get_local_ip()
    if local_ip and local_ip not in ips:
        ips.insert(0, local_ip)
    
    return ips

if __name__ == '__main__':
    print("=" * 60)
    print("YOUR PC'S IP ADDRESS FOR REMOTE ACCESS")
    print("=" * 60)
    print()
    
    ips = get_all_ips()
    
    if not ips:
        print("❌ Could not determine IP address")
        print()
        print("You can find it manually:")
        print("   Windows: Open Command Prompt and run: ipconfig")
        print("   Look for 'IPv4 Address' under your active network adapter")
        sys.exit(1)
    
    print("📱 Use these addresses from your phone:")
    print()
    
    for i, ip in enumerate(ips, 1):
        print(f"   Option {i}: {ip}")
        print(f"      Dashboard: http://{ip}:5000")
        print(f"      Token Update: http://{ip}:5000/token-update")
        print()
    
    print("=" * 60)
    print("INSTRUCTIONS:")
    print("=" * 60)
    print()
    print("1. Make sure your phone is on the SAME WiFi network as this PC")
    print("2. Open your phone's browser")
    print("3. Go to: http://" + ips[0] + ":5000/token-update")
    print("4. Follow the instructions on the page to update your token")
    print()
    print("⚠️  If you can't connect:")
    print("   - Make sure Windows Firewall allows port 5000")
    print("   - Make sure both devices are on the same network")
    print("   - Try the other IP addresses listed above")
    print()
    print("=" * 60)
    input("Press Enter to close...")

