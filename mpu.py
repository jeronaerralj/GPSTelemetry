import time
import board
import adafruit_mpu6050
import busio

i2c = busio.I2C(board.SCL, board.SDA)

try:
    # Try default address 0x68
    mpu = adafruit_mpu6050.MPU6050(i2c)
    print("Found MPU6050 at 0x68")
except:
    try:
        # Try alternative address 0x69
        mpu = adafruit_mpu6050.MPU6050(i2c, address=0x68)
        print("Found MPU6050 at 0x68")
    except Exception as e:
        print("Failed to find MPU6050!")
        print(f"Error: {e}")
        exit()

while True:
    print(f"Accel: {mpu.acceleration}")
    time.sleep(1)