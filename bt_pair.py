import pexpect
import sys

child = pexpect.spawn('bluetoothctl', timeout=60, encoding='utf-8')
child.logfile = sys.stdout

# Strip ANSI codes in pattern matching by waiting for known output strings
child.expect(['Agent registered', pexpect.TIMEOUT], timeout=5)
child.sendline('agent NoInputNoOutput')
child.expect(['Agent registered', 'registered', pexpect.TIMEOUT], timeout=5)
child.sendline('default-agent')
child.expect(['agent', pexpect.TIMEOUT], timeout=5)
child.sendline('discoverable on')
child.expect(['succeeded', pexpect.TIMEOUT], timeout=5)
child.sendline('pairable on')
child.expect(['succeeded', pexpect.TIMEOUT], timeout=5)

print("\n\nWaiting for pairing request from phone (60s)...")
try:
    idx = child.expect(['Confirm passkey', 'Request confirmation', 'Paired: yes', 'AuthenticationFailed'], timeout=60)
    if idx in [0, 1]:
        child.sendline('yes')
        print("\nAccepted pairing!")
        child.expect(r'\[bluetooth\]|#', timeout=15)
    elif idx == 2:
        print("\nAlready paired!")
except pexpect.TIMEOUT:
    print("\nNo pairing request received.")
finally:
    child.sendline('quit')
    child.close()
