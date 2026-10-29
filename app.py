import threading
import time
import json
import math
import struct
from flask import Flask, render_template, jsonify

# --- HARDWARE IMPORTS ---
try:
    import board
    import busio
    import adafruit_ssd1306
    from PIL import Image, ImageDraw, ImageFont
    from gpiozero import InputDevice, LED, Buzzer
    import pyttsx3 
    HARDWARE_AVAILABLE = True
except ImportError as e:
    print(f"Hardware libraries missing: {e}")
    HARDWARE_AVAILABLE = False

# --- CONFIGURATION ---
USE_SIMULATOR = False 
BAUD_RATE = 9600
SERIAL_PORT = '/dev/serial0'   

# Hardware Pins
PIN_LED = 17    
PIN_BUZZER = 27 
PIN_BUTTON = 22 

# Global storage
gps_data = {
    "lat": 0.0, "lon": 0.0, "alt": 0.0, "speed": 0.0,
    "accel": 1.0, 
    "num_sats": 0, "timestamp": "Waiting...", "fix_quality": 0
}
gps_path = [] 
data_lock = threading.Lock()

app = Flask(__name__)

# ---------------------------------------------------------
# CUSTOM MPU6050 DRIVER (CLONE FRIENDLY)
# ---------------------------------------------------------
class MPU6050_Minimal:
    def __init__(self, i2c, address=0x68):
        self.i2c = i2c
        self.address = address
        self.buffer = bytearray(2)
        # Wake up the chip (Write 0 to PWR_MGMT_1 register 0x6B)
        self._write_register(0x6B, 0x00)

    def _write_register(self, register, value):
        self.buffer[0] = register
        self.buffer[1] = value
        while not self.i2c.try_lock():
            pass
        try:
            self.i2c.writeto(self.address, self.buffer)
        finally:
            self.i2c.unlock()

    def _read_registers(self, start_register, count):
        buf = bytearray(count)
        while not self.i2c.try_lock():
            pass
        try:
            self.i2c.writeto_then_readfrom(self.address, bytes([start_register]), buf)
        finally:
            self.i2c.unlock()
        return buf

    @property
    def acceleration(self):
        # Read 6 bytes starting from ACCEL_XOUT_H (0x3B)
        raw_data = self._read_registers(0x3B, 6)
        # Unpack 3 signed short integers (big-endian)
        vals = struct.unpack(">hhh", raw_data)
        # Convert raw values to m/s^2 (Standard gravity range +/- 2g is 16384 LSB/g)
        # 9.80665 is gravity. 16384 is the scale factor.
        return (
            (vals[0] / 16384.0) * 9.80665,
            (vals[1] / 16384.0) * 9.80665,
            (vals[2] / 16384.0) * 9.80665
        )

# ---------------------------------------------------------
# HARDWARE CONTROLLER
# ---------------------------------------------------------
def hardware_loop():
    if not HARDWARE_AVAILABLE: return

    try:
        # 1. Initialize Components
        led = LED(PIN_LED)
        buzzer = Buzzer(PIN_BUZZER)
        button = InputDevice(PIN_BUTTON, pull_up=True) 
        
        engine = pyttsx3.init()
        engine.setProperty('rate', 150)
        
        # I2C Bus
        i2c = busio.I2C(board.SCL, board.SDA)
        
        # OLED (0x3C)
        disp = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c, addr=0x3C)
        
        # MPU6050 (0x68) - Using Manual Driver
        try:
            mpu = MPU6050_Minimal(i2c, address=0x68)
            print("MPU6050 initialized (Clone Mode)")
        except Exception as e:
            print(f"MPU Error: {e}")
            mpu = None

        # Graphics Setup
        disp.fill(0); disp.show()
        width = disp.width; height = disp.height
        image = Image.new("1", (width, height))
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        
        has_fix = False
        last_speak_time = 0

        print("Hardware Controller Started...")

        while True:
            draw.rectangle((0, 0, width, height), outline=0, fill=0)
            
            # 2. Safe Sensor Reading
            g_force = 1.0
            if mpu:
                try:
                    acc_x, acc_y, acc_z = mpu.acceleration
                    g_force = math.sqrt(acc_x**2 + acc_y**2 + acc_z**2) / 9.80665
                except Exception:
                    pass # Ignore read errors
            
            with data_lock:
                gps_data['accel'] = g_force
                lat = gps_data['lat']; lon = gps_data['lon']
                sats = gps_data['num_sats']; quality = gps_data['fix_quality']

            # 3. Button Logic
            if button.value == 0:
                if time.time() - last_speak_time > 2:
                    last_speak_time = time.time()
                    text = f"Lat {lat:.3f}, Lon {lon:.3f}" if lat != 0 else "Searching signal."
                    engine.say(text); engine.runAndWait()

            # 4. Display Logic
            if lat != 0.0 and quality > 0:
                if not has_fix:
                    has_fix = True
                    led.on(); buzzer.beep(0.1, 0.1, 2)
                
                draw.text((0, 0),  f"SATS: {sats} | FIX: YES", font=font, fill=255)
                draw.text((0, 15), f"LAT: {lat:.5f}", font=font, fill=255)
                draw.text((0, 25), f"G-FORCE: {g_force:.2f}g", font=font, fill=255)
                draw.text((0, 50), "BTN: READ VOICE", font=font, fill=255)
            else:
                has_fix = False
                led.value = int(time.time() * 2) % 2 
                draw.text((30, 20), "SEARCHING...", font=font, fill=255)
                draw.text((30, 35), f"Tilt: {g_force:.2f}g", font=font, fill=255)
                draw.rectangle((0, 55, int(time.time()*20)%128, 60), outline=255, fill=255)

            disp.image(image); disp.show()
            time.sleep(0.1)

    except Exception as e:
        print(f"Hardware Loop Crash: {e}")

# ---------------------------------------------------------
# GPS HANDLERS
# ---------------------------------------------------------
def update_gps_history():
    global gps_path
    entry = {
        'lat': gps_data['lat'],
        'lng': gps_data['lon'],
        'alt': gps_data['alt'],
        'speed': gps_data['speed'],
        'accel': gps_data['accel'],
        'time': gps_data['timestamp']
    }
    gps_path.append(entry)
    if len(gps_path) > 100: 
        gps_path.pop(0)

def read_gps_serial():
    global gps_data
    try:
        import serial, pynmea2
        with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) as ser:
            while True:
                try:
                    line = ser.readline().decode('ascii', errors='replace').strip()
                    if line.startswith(('$GPGGA', '$GPRMC')):
                        msg = pynmea2.parse(line)
                        if msg.is_valid:
                            with data_lock:
                                if line.startswith('$GPGGA'):
                                    gps_data['lat'] = float(msg.latitude)
                                    gps_data['lon'] = float(msg.longitude)
                                    gps_data['alt'] = msg.altitude
                                    gps_data['num_sats'] = int(msg.num_sats)
                                    gps_data['timestamp'] = str(msg.timestamp)
                                    gps_data['fix_quality'] = msg.gps_qual
                                if line.startswith('$GPRMC'):
                                    gps_data['speed'] = msg.sog_k
                                    gps_data['lat'] = float(msg.latitude)
                                    gps_data['lon'] = float(msg.longitude)
                                
                                if gps_data['lat'] != 0.0:
                                    update_gps_history()
                except: pass
    except Exception as e: print(f"Serial Error: {e}")

def simulate_gps_data():
    global gps_data
    center_lat, center_lon, angle = 14.5995, 120.9842, 0
    while True:
        with data_lock:
            gps_data['lat'] = center_lat + (math.sin(math.radians(angle)) * 0.005)
            gps_data['lon'] = center_lon + (math.cos(math.radians(angle)) * 0.005)
            gps_data['alt'] = 45.0 + (math.sin(math.radians(angle*3)) * 10) 
            gps_data['speed'] = 12.5 + (math.cos(math.radians(angle*2)) * 5) 
            gps_data['accel'] = 1.0 + (math.sin(math.radians(angle*10)) * 0.2) 
            gps_data['num_sats'] = 8
            gps_data['timestamp'] = time.strftime("%H:%M:%S", time.gmtime())
            gps_data['fix_quality'] = 1
            update_gps_history()
        angle = (angle + 5) % 360
        time.sleep(1)

# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/gps')
def get_gps_data():
    with data_lock:
        response = gps_data.copy()
        response['path'] = gps_path
        return jsonify(response)

if __name__ == '__main__':
    threading.Thread(target=hardware_loop, daemon=True).start()
    if USE_SIMULATOR:
        gps_thread = threading.Thread(target=simulate_gps_data)
    else:
        gps_thread = threading.Thread(target=read_gps_serial)
    
    gps_thread.daemon = True
    gps_thread.start()
    
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)