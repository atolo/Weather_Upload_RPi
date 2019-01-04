# This mododule downloads data from Weather Underground, specifically rain accumulation for the day and pressure
# This module has the following functions
# getDailyRain() - used on reboot to get the daily rain accumulation for the day.  This sets the starting point for the rain counter
# getPressure() - connects to nearby weather station to get the pressure since my station has no baromoter wireless data,
#                 the baromoter is in the console and that info isn't available
# Weather Underground API Response fields: https://www.wunderground.com/weather/api/d/docs?d=data/conditions
#  
# For West Dover data instead of quering specivic station IDs, you could do something like this
# http://api.wunderground.com/api/ APIKEY /conditions/q/VT/west-wardsboro/05356.json


WU_STATIONS = ["KVTWESTD4", "KVTDOVER8"]  # weather station IDs to get pressure data

import requests        # Allows you to send HTTP/1.1 requests
import WU_credentials  # Weather underground password, station IDs and API key

ERR_INVALID_DATA = -102

apiKey = WU_credentials.WU_API_KEY


# Get daily rain data from Suntec station.  Need this on reboot of RPi
# Returns inches of rain since midnight
def getDailyRain():
    getUrl = "http://api.wunderground.com/api/" + apiKey + "/geolookup/conditions/q/pws:KVTWESTD3.json"
    response = requests.get(getUrl).json()
    if len(response) > 1:
        if isNumber(response['current_observation']['precip_today_in']):
            daily_rain = float(response['current_observation']['precip_today_in'])
            print('Suntec station daily rain={}'.format(daily_rain))  # srg debug print
            return(daily_rain)

    return(ERR_INVALID_DATA)

def getPressure():
    i = 0
    while i < len(WU_STATIONS):  # loops through stations in WU_STATIONS list
        # Get pressure from nearby station  
        getUrl = "http://api.wunderground.com/api/" + apiKey + "/geolookup/conditions/q/pws:" + WU_STATIONS[i] + ".json"

        response = requests.get(getUrl).json()
        if len(response) > 1: # valid response returns 3, if there's an error, the len() is 1
            if isNumber(response['current_observation']['pressure_in']):
                nearby_pressure = float(response['current_observation']['pressure_in'])
                nearby_last_update_time = int(response['current_observation']['observation_epoch'])
                if(nearby_pressure) > 25: # a pressure less than 25 inHg isn't gonna be valid
#                    print ('pressure={} (from {})'.format(nearby_pressure, WU_STATIONS[i])) # srg debug print
                    return(nearby_pressure)

        # Didn't get a valid pressure. Try the next station in WU_STATIONS tuple
#        print ('Failed to get pressure data from {}'.format(WU_STATIONS[i]))
        nearby_pressure = ERR_INVALID_DATA
        nearby_last_update_time = 0
        i += 1
        
    # Couldn't get pressure, return an error
    return(ERR_INVALID_DATA)
        


# Checks to see if a string is numeric
def isNumber(str):
    try:
        float(str)
        return True
    except ValueError:
        return False


    


