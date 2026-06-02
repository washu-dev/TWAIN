import json
import sqlite3

DB_NAME = 'executionlog.db'

# Initializes the database
def init_db():
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute('''CREATE TABLE jobs(
                submissionId TEXT PRIMARY KEY,
                jobName TEXT NOT NULL,
                submitTime TEXT NOT NULL,
                startTime TEXT,
                endTime TEXT,
                status TEXT NOT NULL, -- QUEUED, RUNNING, SUCCESS, FAILURE
                errorMessage TEXT,
                metadata TEXT,
                )''')
    con.commit()

# Gets the status of a job from an id
def status(specifiedJobId):
    # Opening database and getting status of specified job
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    query = '''SELECT status FROM jobs WHERE jobId = ?'''
    cur.execute(query, (specifiedJobId,))
    result = cur.fetchone()

    # Closing database
    con.commit()
    con.close()

    # Returning result
    if result is None:
        return "No job matching specified job id"
    else:
        return result

# Data is json file with 
def jobQueued(data):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()