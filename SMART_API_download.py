import json
import pandas as pd
import requests
import time
import os
from os import getenv
from dotenv import load_dotenv
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

#Library from https://github.com/ncl-icb-analytics/sqlsnippets
#pip install ncl-sqlsnippets
import ncl_sqlsnippets as snips

#Process settings
def import_settings():
    load_dotenv(override=True)

    return {
        "API_URL": getenv("API_URL"),
        "API_KEY": getenv("API_KEY"),
        
        "DATE_END": getenv("DATE_END"),
        "DATE_WINDOW": getenv("DATE_WINDOW"),
        
        "WAIT_PERIOD": getenv("WAIT_PERIOD"),
        "WAIT_COOLOFF": getenv("WAIT_COOLOFF"),
        
        "SITES": json.loads(getenv("SITES")),

        "SQL_ADDRESS": getenv("SQL_ADDRESS"),
        "SQL_DATABASE": getenv("SQL_DATABASE"),
        "SQL_SCHEMA": getenv("SQL_SCHEMA"),
        "SQL_TABLE": getenv("SQL_TABLE")
    }

#Sets the directory to the location of this file
def set_cwd():
    #Change working directory to current
    script_dir = os.path.dirname(__file__)
    os.chdir(script_dir)

#Takes the input DATE_END and returns it as a date type variable
def process_date_end(input_date_end):
    #Check for keyword to use current date
    if input_date_end == "today":
        date_end = datetime.now().date()
    else:
        #Check a valid date was used
        try:
            date_end = datetime.strptime(input_date_end, "%Y-%m-%d")
        except:
            raise Exception(f"Unrecognised DATE_END in env file: {input_date_end}")

    return date_end

#Takes the input DATE_WINDOW and returns the date_start as a date type variable
def process_date_window(window, date_end):
    #If a number is given then assume it is in terms of days
    if isinstance(window, int):
        return date_end - timedelta(days=window-1)
    
    #If window is written:
    input_window = window.split(" ")

    #Sanitise input
    if len(input_window) != 2:
        raise Exception(f"The window type {window} is not formatted correctly.")
    
    if input_window[1].endswith('s'):
        input_window[1] = input_window[1][:-1]

    #Process window value to get date_start
    if input_window[1] == "day":
        return date_end - timedelta(days = int(input_window[0]) - 1)
    
    elif input_window[1] == "week":
        return date_end - timedelta(days = (int(input_window[0]) * 7) - 1)
    
    elif input_window[1] == "month":
        return date_end - relativedelta(months = int(input_window[0]))
    
    elif input_window[1] == "year":
        return date_end - relativedelta(years = int(input_window[0]))
    
    else:
        raise Exception(f"The window type {window.split(' ')[1]} is not supported.")

#Calculate array of runs to perform
def calculate_runs(date_start, date_end):
    
    #The number of days between the date_end and date_start
    window_days = (date_end - date_start).days 

    #Array to store run windows
    runs = []
    #Cursors to track date through iteration
    date_cursor_live = date_end
    date_cursor_prev = date_end

    #Iterate to create runs of 7 days
    while window_days > 7:
        #Move cursor back 6 days
        date_cursor_live = date_cursor_prev - timedelta(days=6)
        #Add run to array
        runs.append([
                    datetime.strftime(date_cursor_live, "%Y-%m-%d"), 
                    datetime.strftime(date_cursor_prev, "%Y-%m-%d")
                    ])
        #Update window_days to track when to stop creating runs
        window_days = (date_cursor_live - date_start).days
        #Record where the cursor should start for the next run
        date_cursor_prev = date_cursor_live - timedelta(days=1)
    
    #Add final run for leftover days
    runs.append([
                datetime.strftime(date_start, "%Y-%m-%d"), 
                datetime.strftime(date_cursor_prev, "%Y-%m-%d")
                ])

    return runs

#Make a request to the API
def smart_request (url, key, date_start, date_end, hash_id):
    #Set request URL
    req_url = f"{url}api/sitrep/site/{hash_id}/"

    #Set request parameters
    params = {
        "key":key,
        "date_start": date_start,
        "date_end": date_end
    }

    #Set request headers
    headers = {
        "Content-Type": "application/json"
    }

    #Make request
    response = requests.get(req_url, params=params, headers=headers)

    #Check for response status
    if response.status_code == 200:
        data = json.loads(response.text)
        df = pd.DataFrame(data['OUTPUT'])

        return df

    else:
        # Handle the error
        print(f"Error: {response.status_code}")
        raise Exception (response.text) 

#Force delay between requests
def add_delay(seconds):
    time.sleep(int(seconds))

#Build the delete query to remove duplicate data
def get_delete_query(date_start, date_end, site, env):

    sql_database =  env["SQL_DATABASE"]
    sql_schema =  env["SQL_SCHEMA"]
    sql_table =  env["SQL_TABLE"]

    query = f"""
                DELETE FROM [{sql_database}].[{sql_schema}].[{sql_table}] 
                WHERE reportDate >= '{date_start}' 
                    AND reportDate <= '{date_end}' 
                    AND siteId = '{site}'
                """
    
    return query

#Upload the request data
def upload_request_data(data, date_start, date_end, site, env):

    #Delete existing data
    query_del = get_delete_query(date_start, date_end, site, env)

    #Upload the data
    try:
        #Connect to the database
        engine = snips.connect(env["SQL_ADDRESS"], env["SQL_DATABASE"])
        if (snips.table_exists(engine, env["SQL_TABLE"], env["SQL_SCHEMA"])):
            #Delete the existing data
            snips.execute_query(engine, query_del)
        #Upload the new data
        snips.upload_to_sql(data, engine, env["SQL_TABLE"], env["SQL_SCHEMA"], replace=False, chunks=150)
    except:
        print("Disconnected from the sandpit. Waiting before trying again...")
        #If the connection drops, wait and try again
        add_delay(env["WAIT_COOLOFF"])

        try:
            #Connect to the database
            engine = snips.connect(env["SQL_ADDRESS"], env["SQL_DATABASE"])
            if (snips.table_exists(engine, env["SQL_TABLE"], env["SQL_SCHEMA"])):
                #Delete the existing data
                snips.execute_query(engine, query_del)
            #Upload the new data
            snips.upload_to_sql(data, engine, env["SQL_TABLE"], env["SQL_SCHEMA"], replace=False, chunks=150)
        except:
            raise Exception("Connectioned dropped again so cancelling execution")

    print(f"Upload successful for site {site} from {date_start} to {date_end}")

#Execute runs
def execute_runs(runs, env):

    #Get request variables
    url = env["API_URL"]
    key = env["API_KEY"]
    hash_sites = env["SITES"]
    delay = env["WAIT_PERIOD"]
    cooloff = env["WAIT_COOLOFF"]

    #Set  True initially so no delay on first request
    init = True

    #Iterate through runs to get all of the data
    for run in runs:

        #Get dates for the run
        date_start = run[0]
        date_end = run[1]

        #Make a get request per site
        for site in hash_sites:

            #Delay after 1st run to prevent Too Many Requests Error
            if init:
                init = False
            else:
                add_delay(delay)

            try:
                res = smart_request(url, key, date_start, date_end, site)
            except:
                print("Overload so waiting...")
                add_delay(cooloff)
                try:
                    res = smart_request(url, key, date_start, date_end, site)
                except:
                    raise Exception("Failed twice so cancelling execution.")
            #print(f"Request fulfilled for site {site} from {date_start} to {date_end}")

            #Upload and manage datasets
            upload_request_data(res, date_start, date_end, site, env)

#Main function
def main():
    
    #Set file location as cwd
    set_cwd()

    #Import settings from the .env file
    env = import_settings()

    #Process the settings to get the start and end dates
    date_end = process_date_end(env["DATE_END"])
    date_start = process_date_window(env["DATE_WINDOW"], date_end)

    #Determine how many runs are needed to get all the data
    runs = calculate_runs(date_start, date_end)

    #Execute the runs on the API and upload the result to the sandpit
    execute_runs(runs, env)

print("Program starting...")
main()
