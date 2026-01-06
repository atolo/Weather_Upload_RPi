# Directly connects Raspberry Pi with RS485 module to Davis Vantage Pro2 wired 6322C weather station
# and sends it to Weather Underground.
# Forked from: https://github.com/Scott216/Weather_Upload_RPi

### Change Log ###
# 10/24/2024. Initial fork.
# Added serial connection and removed i2c and moteino. Added BME280 and pressure calculations.

version = "v1.31"

import time
#import smbus  # Used by I2C
import os.path # used to see if a file exist
import math # Used by humidity calculation
import board
from adafruit_bme280 import basic as adafruit_bme280
#import RPi.GPIO as GPIO # reads/writes GPIO pins
import WU_credentials # Weather underground password, API key and station IDs
import WU_download  # downloads daily rain on startup, and pressure from other weather staitons
import WU_upload  # uploads data to Weather Underground
import WU_decodeData # Decodes wireless data coming from Davis ISS weather station
import weatherData_cls # class to hold weather data for the Davis ISS station
from subprocess import check_output # used to print RPi IP address
import serial

# Configuration constants
debug = True
ELEVATION_METERS = 26  # Replace with your actual elevation in meters
MIN_VALID_PRESSURE_INHG = 25.0  # Minimum valid pressure reading in inches of Hg
UPLOAD_FREQUENCY_SECONDS = 5  # Seconds between uploads to Weather Underground
DETAIL_STATS_INTERVAL = 60  # Seconds between detail stats logging
NO_UPLOAD_THRESHOLD = 300  # Seconds threshold for no upload warning
SERIAL_BAUDRATE = 4800  # Davis weather station baud rate
SERIAL_TIMEOUT = 3  # Serial port read timeout in seconds

ISS_STATION_ID = 1
WU_STATION = WU_credentials.WU_STATION_ID_SUNTEC # Main weather station
# WU_STATION = WU_credentials.WU_STATION_ID_TEST # Test weather station

# Instantiate suntec object from weatherStation class (weatherData_cls.py)
suntec = weatherData_cls.weatherStation(ISS_STATION_ID)

# Header byte 0, 4 MSB describe data in bytes 3-4
ISS_WIND_SPEED   = 0x1
ISS_CAP_VOLTS    = 0x2
ISS_UV_INDEX     = 0x4
ISS_RAIN_SECONDS = 0x5
ISS_SOLAR_RAD    = 0x6
ISS_OUT_TEMP     = 0x8
ISS_WIND_GUST    = 0x9
ISS_HUMIDITY     = 0xA
ISS_RAIN_COUNT   = 0xE

# Configure the serial port
ser = serial.Serial(
    port='/dev/serial0',  # Replace with your serial port
    baudrate=SERIAL_BAUDRATE,
    timeout=SERIAL_TIMEOUT
)

# Initialize BME280 sensor once at startup (retry a few times in case I2C not ready)
bme280_initialized = False
bme280 = None
for attempt in range(5):
    try:
        i2c = board.I2C()  # uses board.SCL and board.SDA
        bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=0x76)
        bme280.sea_level_pressure = 1013.25
        bme280_initialized = True
        print("BME280 sensor initialized successfully")
        break
    except Exception as e:
        print(f"Warning: Failed to initialize BME280 sensor (attempt {attempt+1}/5): {e}")
        time.sleep(1)

if not bme280_initialized:
    bme280 = None
    print("Warning: BME280 sensor not initialized after retries")

#---------------------------------------------------------------------
# Validate weather data from wireless packet
#---------------------------------------------------------------------
def decodeRawData(packet):
    if (debug):
        print(f"Decoding packet: {' '.join([f'{b:02x}' for b in packet])}")
    
    # check CRC
    if WU_decodeData.crc16_ccitt(packet) == False:
        print("CRC check failed")
        return (False, "Invalid CRC")
    
    packetStationID = WU_decodeData.stationID(packet)
    if packetStationID != suntec.stationID:
        print(f"Wrong station ID. Expected {suntec.stationID} but got {packetStationID}")
        return (False, f"Wrong station ID. Expected {suntec.stationID} but got {packetStationID}")
    
    # Wind speed is in every packet
    newWindSpeed = WU_decodeData.windSpeed(packet)
    if (newWindSpeed >= 0):
        suntec.windSpeed = newWindSpeed
    else:
        errmsg = 'Error extracting wind speed from packet. Got {} from {}'.format(newWindSpeed, packet) 
        suntec.windSpeed  = 0
        return[False, errmsg] # error extracting wind speed, stop processing packet
    
    # Wind direction is in every packet
    newWindDir = WU_decodeData.windDirection(packet)
    if (newWindDir >= 0):
        suntec.windDir = newWindDir
        suntec.avgWindDir(newWindDir)
    else:
        errmsg = 'Error extracting wind direction from packet. Got {} from {}'.format(newWindDir, packet) 
        return[False, errmsg] # Error extracting wind direction, stop processing packet
    
    # Extract the data type from the packet header
    dataSent = packet[0] >> 4

    if dataSent == ISS_RAIN_COUNT:
        # Handle rain count data
        global g_rainCounterOld  # convert from local to global variable
        global g_rainCntDataPts  # convert from local to global variable
        
        rainCounterNew = WU_decodeData.rainCounter(packet)
        if rainCounterNew < 0 or rainCounterNew > 127:
            print('Invalid rain counter value:{} from {}'.format(rainCounterNew, packet))
            return (False, "Invalid rain counter value")
        
        # Don't calculate rain counts until RPi has received 2nd data point.  First data point will be the
        # starting value, then 2nd data point will be the accumulation, if any.  For example, if first time
        # data arrives its 50, we don't want to take 50-0 = 50 (ie 0.5") and add that to the daily rain accumulation.
        # Wait until the next data point comes in, which will probably be 50 (in this example), so 50-50 = 0.  No rain accumulated.
        # If it's raining at the time of reboot, you might get 51, so 51 - 50 = 1 or 0.01" added.
        if (g_rainCntDataPts == 1):
            g_rainCounterOld = rainCounterNew
            
        if (g_rainCntDataPts >= 2) and (g_rainCounterOld != rainCounterNew):

            # See how many bucket tips counter went up.  Should be only one unless it's 
            # raining really hard or there is a long transmission delay from ISS
            if (rainCounterNew < g_rainCounterOld):
                newRain = (128 - g_rainCounterOld) + rainCounterNew # Rain counter has rolled over (counts from 0 - 127)
            else:
                newRain = rainCounterNew - g_rainCounterOld
            
            suntec.rainToday += newRain/100.0;  # Increment daily rain counter
            g_rainCounterOld = rainCounterNew
                
        g_rainCntDataPts += 1 # Increment number times RPi received rain count data

        return (True, "Rain count data processed")

    elif dataSent == ISS_RAIN_SECONDS:
        # Handle rain rate data
        rainSeconds = WU_decodeData.rainRate(packet) # seconds between bucket tips, 0.01" per tip
        fifteenMin = 60 * 15 # seconds in 15 minutes
        if rainSeconds > 0: #If no error
            if (rainSeconds < fifteenMin):
                suntec.rainRate = (0.01 * 3600.0) / rainSeconds
            else:
                suntec.rainRate = 0.0 # More then 15 minutes since last bucket tip, can't calculate rain rate until next bucket tip
            return (True, "Rain rate data processed")
        print('Invalid rain seconds. Got {} from {}'.format(rainSeconds, packet))
        return (False, "Invalid rain seconds")

    elif dataSent == ISS_OUT_TEMP:
        # Handle temperature data
        newTemp = WU_decodeData.temperature(packet)
        if newTemp > -100: #If no error
            suntec.outsideTemp = newTemp
            suntec.calcWindChill() # calculate windchill
            # If we have R/H too, then calculate dew point
            if suntec.gotHumidityData():
                newDewPoint = suntec.calcDewPoint() # Calculate dew point
                if (newDewPoint <= -100): 
                    print('Invalid dewpoint: {} from temp={} and humidity={}'.format(newDewPoint, suntec.outsideTemp, suntec.humidity))
            return (True, "Temperature data processed")
        else: 
            print('Invalid temperature. Got {} from {}'.format(newTemp, packet))
            return (False, "Invalid temperature")

    elif dataSent == ISS_WIND_GUST:
        # Handle wind gust data
        newWindGust = WU_decodeData.windGusts(packet)
        if newWindGust >= 0:
            suntec.windGust = newWindGust
            return (True, "Wind gust data processed")
        print('Invalid wind gust. Got {} from {}'.format(newWindGust, packet))
        return (False, "Invalid wind gust")

    elif dataSent == ISS_HUMIDITY:
        # Handle humidity data
        newHumidity = WU_decodeData.humidity(packet)
        if newHumidity > 0:
            suntec.humidity = newHumidity
            # If we have outside temperature too, then calculate dew point
            if suntec.gotTemperatureData():
                newDewPoint = suntec.calcDewPoint() # Calculate dew point
                if (newDewPoint <= -100): 
                    print('Invalid dewpoint: {} from temp={} and humidity={}'.format(newDewPoint, suntec.outsideTemp, suntec.humidity))

            return (True, "Humidity data processed")
        print('Invalid humidity. Got {} from {}'.format(newHumidity, packet))
        return (False, "Invalid humidity")

    elif dataSent == ISS_CAP_VOLTS:
        # Handle capacitor voltage data
        newCapVolts = WU_decodeData.capVoltage(packet)
        if newCapVolts >= 0:
            suntec.capacitorVolts = newCapVolts
            return (True, "Capacitor voltage data processed")
        else:
            print('Invalid cap volts.  Got {} from {}'.format(newCapVolts, packet))
            suntec.capacitorVolts = -1
            return (False, "Invalid cap volts")
            
    elif dataSent == ISS_SOLAR_RAD:
        # Handle solar radiation data
        solarRad = WU_decodeData.solarRadiation(packet)
        if solarRad >= 0:
            suntec.solar = solarRad
            return (True, "Solar radiation data processed")
        else:
            print(f'Invalid solar radiation. Got {solarRad} from {packet}')
            return (False, "Invalid solar radiation")

    elif dataSent == ISS_UV_INDEX:
        # Handle UV index data
        uvIndex = WU_decodeData.uvIndex(packet)
        if uvIndex >= 0:
            suntec.uvIndex = uvIndex
            return (True, "UV index data processed")
        else:
            print(f'Invalid UV index. Got {uvIndex} from {packet}')
            return (False, "Invalid UV index")

    else:
        print(f"Unhandled data type: 0x{dataSent:X}")
        return (False, f"Unhandled data type: 0x{dataSent:X}")
    
    return (True, f"Processed data type: 0x{dataSent:X}")
#---------------------------------------------------------------------
# Get Pressure from BME280 sensor
#---------------------------------------------------------------------
def getAtmosphericPressure():

    if not bme280_initialized or bme280 is None:
        print("BME280 sensor not initialized")
        return -1
    
    try:
        # Define the elevation in meters
        elevation = ELEVATION_METERS
        # BME280 returns pressure in hPa (hectopascals), where 1 hPa = 1 millibar
        pressure_hpa = bme280.pressure
        
        # Adjust to sea level pressure using barometric formula
        # Formula: P_sea = P_station / (1 - h/44330)^5.255
        pressure_sea_level_mb = pressure_hpa / ((1 - (elevation / 44330.0)) ** 5.255)
        
        # Convert millibars to inches of mercury (1 mb = 0.02953 inHg)
        pressure_inches = pressure_sea_level_mb * 0.02953
        rounded_pressure_inches = round(pressure_inches, 2)
        
        return(rounded_pressure_inches)
    except Exception as e:
        print(f"Error reading BME280 sensor: {e}")
        return -1

#---------------------------------------------------------------------
# Prints uploaded weather data
#---------------------------------------------------------------------
def printWeatherDataTable(printRawData=None):

    global g_TableHeaderCntr1
    dataType = ["0x0", "0x1", "Super Cap", "0x3", "UV Index", "Rain Seconds", "Solar Radiation", "Solar Cell Volts", \
                "Temperature", "Gusts", "Humidity", "0xB", "0xC", "0xD", "Rain Counter", "0xF"]
    
    windDirNow = (g_rawDataNew[2] * 1.40625) + 0.3
    
    strHeader =  'temp\tR/H\tpres\twind\tgust\t dir\tavg\trrate\ttoday\t dew\ttime stamp'
    strSummary = '{0.outsideTemp}\t{0.humidity}\t{0.pressure}\t {0.windSpeed}\t {0.windGust}\t {1:03.0f}\t{0.windDir:03.0f}\t{0.rainRate:.2f}\t{0.rainToday:.2f}\t {0.dewPoint:.2f}\t' \
                 .format(suntec, windDirNow) + time.strftime("%m/%d/%Y %I:%M:%S %p")

    if (printRawData == True):
        strHeader = strHeader + '\t\t raw wireless data'
        strSummary = strSummary + "   " + ''.join(['%02x ' %b for b in g_rawDataNew]) + "("  + dataType[g_rawDataNew[0] >> 4] + ")"
    
    if (g_TableHeaderCntr1 == 0):
        print(strHeader)
        g_TableHeaderCntr1 = 20 # reset header counter
    print(strSummary)
    
    g_TableHeaderCntr1 -= 1

    logFile(False, "Data", strSummary) # append data (first param False if append vs. write) to log file



#---------------------------------------------------------------------
# Create or append log files: weather data and errors
# newFile = True, then a new file will be created;'w' parameter in open()).  This would be at
# midnight every day, and sometimes when program is restarted.
# If newFile = False, then data should be appended to existing file; 'a" parameter in open().
#
# logType is either "Data" or "Error"
#
# logData is data to be appended to the log file
#---------------------------------------------------------------------
def logFile(newFile, logType, logData):

    datafilename =  "Logs/Upload Data_" + time.strftime("%y%m%d") + ".txt"
    errorfilename = "Logs/Error log_"   + time.strftime("%y%m%d") + ".txt"

    if (newFile == True):
        # Create new data log file
        if not os.path.exists(datafilename):
            # If data log doesn't exist, create it and add header
            with open(datafilename, "w") as datalog:
                strHeader =  "temp\tR/H\tpres\twind\tgust\t dir\tavg\trrate\ttoday\t dew\ttime stamp\n"
                datalog.write(strHeader)
            
        # Create new error log file
        if not os.path.exists(errorfilename):
            # If error log doesn't exist, create it and add header
            with open(errorfilename, "w") as errlog:
                strErrHeader =  "Uploads\t  HTTP Err\tLast U/L Hrs\tISS Err\tISS Avg Min\tISS Age\ttime stamp\n"
                errlog.write(strErrHeader)             

    else: # append data to existing log file
        if(logType == "Data"):
            # log type is data log
            with open(datafilename, "a") as datalog:
                datalog.write(logData)
                datalog.write('\n') # Add eol character

        else: # log type is error log
            with open(errorfilename, "a") as errlog:
                errlog.write(logData)
                errlog.write(time.strftime("%m/%d/%Y %I:%M:%S %p\n"))  # Add timestamp and eol character             
        

#---------------------------------------------------------------------
# Log stats every minute if data isn't being uploaded to W/U
##  - moteino ready
##  - moteino min since last Rx
##  - moteino heartbeat
##  - got dewpoint
##  - last W/U upload (min)
##  - Perf Status
##    - WU Uploads
##    - HTTP Failes
##    - Upload timestamp
##    - I2C Success
##    - I2C Fail
##    - ISS Fail
##    - ISS Success
#---------------------------------------------------------------------
detailStatTimer = time.time() + DETAIL_STATS_INTERVAL  # global variable to print logFileDetail every minute if no w/u uploads
def logFileDetail():

    lastUploadMin = round((time.time() - perfStats[STAT_UPLOAD_TIMESTAMP])/20,2)  # minutes since last W/U upload
    minSinceLastNewISSData = (time.time() - perfStats[STAT_NEW_ISS_TIMESTAMP])/60
##    detailLogData = [g_moteinoReady,
##                     moteinoTimer,
##                     isHeartbeatOK(),
##                     suntec.gotDewPointData(),
##                     lastUploadMin,
##                     minSinceLastNewISSData,
##                     perfStats]
##    print("{}  {}".format(detailLogData, time.strftime("%m/%d/%Y %I:%M:%S %p")))

    detailLogOutput = "{}\t{}\t{}\t{}\t{}\t{:0.1f}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
                                   suntec.gotDewPointData(),
                                   lastUploadMin,
                                   minSinceLastNewISSData,
                                   perfStats[STAT_UPLOADS],
                                   perfStats[STAT_HTTP_FAIL],
                                   perfStats[STAT_ISS_SUCCESS],
                                   perfStats[STAT_ISS_FAIL],
                                   g_rawDataNew,
                                   time.strftime("%m/%d/%Y %I:%M:%S %p")
                                )
    print(detailLogOutput)


    detailErrFilename = "Logs/Detail Error log.txt"
    with open(detailErrFilename, "a") as detErrlog:
        detErrlog.write(detailLogOutput)
        detErrlog.write('\n')


#---------------------------------------------------------------------
# Prints wireless packet data
#---------------------------------------------------------------------
def printWirelessData():

    wirelessData =  ''.join(['%02x ' %b for b in g_rawDataNew])
    print(wirelessData)

#---------------------------------------------------------------------
# Flush serial input
#---------------------------------------------------------------------
def flush_input_buffer():
    ser.reset_input_buffer()
    time.sleep(0.1)  # Short delay to ensure buffer is cleared


#---------------------------------------------------------------------
# Start up 
#---------------------------------------------------------------------
IP = check_output(['hostname', '-I'])
IP = IP.rstrip()  # strips off eol characters
IP = IP.decode('utf-8') # removes b' previx
print("RPi IP Address: {}".format(IP)) 
print("Ver: {}    {}".format(version, time.strftime("%m/%d/%Y %I:%M:%S %p")))

# Create log files for data and errors, First Param = True means to create a new file, vs append to a file
logFile(True, "Data",   "")
logFile(True, "Errors", "")


# Set to zero, weatherStation class initially sets these to -100 for No Data yet
suntec.windGust = 0.0
suntec.rainToday = 0.0

# Get daily rain data from weather station
newRainToday = WU_download.getDailyRain()  # getDailyRain returns a list [0] = success/failure, [1] error message
if newRainToday[0] >= 0:
    print('Suntec station daily rain={}'.format(newRainToday[0]))
    suntec.rainToday = newRainToday[0]
else:
    errMsg = "getDailyRain() error:"
    print("{} {}    {}".format(errMsg, newRainToday[1], time.strftime("%m/%d/%Y %I:%M:%S %p")))
    

# Get pressure from other nearby weather stations
newPressure = getAtmosphericPressure()
if newPressure > MIN_VALID_PRESSURE_INHG:
   suntec.pressure = newPressure
else:
   errMsg = "Error getting pressure data on startup"
   print("{}  {}".format(errMsg,time.strftime("%m/%d/%Y %I:%M:%S %p")))


g_NewISSDataTimeStamp = time.time() + (60 * 10) # Timestamp when last NEW ISS data came in. Default to 10 min from startup
g_SMS_Sent_Today = False  # flag so SMS is only sent once a day
g_SMS_Offline_Msg_Sent = False # flag so SMS is offline message is only sent once
g_rainCounterOld = 0   # Previous value of rain counter, used in def decodeRawData()
g_rainCntDataPts = 0   # Counts the times RPi has received rain counter data, this is not the actual rain counter, thats g_rainCounterOld and rainCounterNew
g_rawDataNew = [0] * 8 # Initialize rawData list. This is weather data that's sent from serial
g_TableHeaderCntr1 = 0 # Used to print header for weather data summary every so often
g_i2cDailyErrors = 0 # Daily counter for I2C errors
g_oldDayOfMonth = int(time.strftime("%d"))   # Initialize day of month variable, used to detect when new day starts
g_tmr_Moteino = time.time()  # Used to request data from moteino every second
tmr_upload = time.time()     # Initialize timer to trigger when to upload to Weather Underground
hourTimer = time.time() + 3600


# List positions for perfStats[] list
STAT_UPLOADS = 0           # 0 - W/U Uploads in last hour
STAT_HTTP_FAIL = 1         # 1 - W/U HTTP failures in last hour
STAT_UPLOAD_TIMESTAMP = 2  # 2 - Timestamp of last successful W/U upload - does not reset every hour
STAT_I2C_SUCCESS = 3       # 3 - I2C success in last hour
STAT_I2C_FAIL = 4          # 4 - I2C failures in last hour
STAT_ISS_FAIL = 5          # 5 - ISS Packet decode errors in last hour
STAT_ISS_SUCCESS = 6       # 6 - Average time (seconds) to receive ISS packet in last hour
STAT_NEW_ISS_TIMESTAMP = 7 # 7 - Timestamp of last time received NEW weather data.  Not reset every hour. This seems to be the main problem when uploads stop - Moteino keeps sending the same packet
perfStats = [0,0,time.time(),0,0,0,0,time.time()]  # list to hold performance stats


#---------------------------------------------------------------------
# Main loop
#---------------------------------------------------------------------

try:
    while True:

        decodeStatus = False # Reset status
        
        try:
            # Wait until at least 8 bytes are available
            if ser.in_waiting >= 8:
                g_rawDataNew = ser.read(8)
                if len(g_rawDataNew) == 8:
                    decodeStatus, decodeMessage = decodeRawData(g_rawDataNew)
                    if decodeStatus:
                        perfStats[STAT_ISS_SUCCESS] += 1
                        print(f"Successfully decoded: {decodeMessage}")
                    else:
                        errMsg = f"Error decoding ISS packet data: {decodeMessage}"
                        print("{}   {}".format(errMsg, time.strftime("%m/%d/%Y %I:%M:%S %p")))
                        print(f"Problematic packet: {' '.join([f'{b:02x}' for b in g_rawDataNew])}")
                        perfStats[STAT_ISS_FAIL] += 1
                else:
                    print("Incomplete packet received")
                    flush_input_buffer()
            else:
                # add a small delay to prevent busy-waiting
                time.sleep(0.1)
        except serial.SerialTimeoutException:
            print("Timeout occurred while reading serial data")
            flush_input_buffer()
        except serial.SerialException as e:
            print(f"Serial error occurred: {e}")
            flush_input_buffer()
            time.sleep(1)  # Longer delay on error
        except Exception as e:
            print(f"An error occurred: {e}")
            print(f"Last packet: {' '.join([f'{b:02x}' for b in g_rawDataNew])}")
        
    
        # If it's a new day, reset daily rain accumulation and I2C Error counter
        newDayOfMonth = int(time.strftime("%d"))
        if newDayOfMonth != g_oldDayOfMonth:
            suntec.rainToday = 0.0
            g_oldDayOfMonth = newDayOfMonth

            # Create new log files for data and errors, First Param = True means to create a new file (False means append to file)
            logFile(True, "Data",   "")
            logFile(True, "Errors", "")


        # If RPi has reecived new valid data from Moteino, and upload timer has passed, and RPi has dewpoint data (note, dewpoint depends on Temp
        # and R/H) then upload new data to Weather Underground
        if ((suntec.gotDewPointData() == True) and (decodeStatus == True) and (time.time() > tmr_upload)):
            newPressure = getAtmosphericPressure() # get latest pressure from bme280
            if (newPressure > MIN_VALID_PRESSURE_INHG):
                suntec.pressure = newPressure  # if a new valid pressure is retrieved, update data. If not, use current value
            printWeatherDataTable(printRawData=False) # print weather data. printRawData parameter deterrmines if raw ISS hex data is also printed.
            
            uploadStatus = WU_upload.upload2WU(suntec, WU_STATION) # upload2WU() returns a list, [0] is succuss/faulure of upload [1] is error message.
            uploadErrMsg = uploadStatus[1]
            # srg debug why uploads stop
            if (time.time() > (perfStats[STAT_UPLOAD_TIMESTAMP] + NO_UPLOAD_THRESHOLD) ):  # srg debug
                print("(debug) HTTP Response: {}".format(uploadStatus))    # srg debug
            if uploadStatus[0] == True:
                perfStats[STAT_UPLOAD_TIMESTAMP] = time.time()
                tmr_upload = time.time() + UPLOAD_FREQUENCY_SECONDS # Set next upload time
                perfStats[STAT_UPLOADS] += 1
                if (debug):
                    print("HTTP Response: {}".format(uploadStatus))
            else:
                errMsg = "Error in upload2WU(), " + uploadErrMsg + ", Last successful upload: {:.1f} minutes ago".format((time.time() - perfStats[STAT_UPLOAD_TIMESTAMP])/60)
                print("{}  {}".format(errMsg,time.strftime("%m/%d/%Y %I:%M:%S %p")))
                perfStats[STAT_HTTP_FAIL] += 1

        # if no upload to W/U for at least 5 min (300 seconds), then print detail data every minute
        if ( (time.time() > detailStatTimer) and ((time.time() - perfStats[STAT_UPLOAD_TIMESTAMP]) > NO_UPLOAD_THRESHOLD)):
            logFileDetail()
            detailStatTimer = time.time() + DETAIL_STATS_INTERVAL # reset timer

        # Every hour print and then reset some stats for debugging
        if (time.time() > hourTimer):
            stats = "   {}\t    {}\t\t  {:.2f}\t\t  {}\t\t  {:.2f}\t  {:.2f}\t\t".format(perfStats[STAT_UPLOADS], perfStats[STAT_HTTP_FAIL], 
                                                                            (time.time() - perfStats[STAT_UPLOAD_TIMESTAMP])/3600, 
                                                                            perfStats[STAT_ISS_FAIL], perfStats[STAT_ISS_SUCCESS]/3600,
                                                                            (time.time() - perfStats[STAT_NEW_ISS_TIMESTAMP])/60 )
            logFile(False, "Error", stats)

            # Reset hourly stats
            perfStats[STAT_UPLOADS] = 0
            perfStats[STAT_HTTP_FAIL] = 0
            perfStats[STAT_ISS_SUCCESS] = 0
            hourTimer = time.time() + 3600

except KeyboardInterrupt:
    print("\nShutting down gracefully...")
    ser.close()
    print("Serial port closed.")
except Exception as e:
    print(f"Fatal error in main loop: {e}")
    ser.close()
    raise


