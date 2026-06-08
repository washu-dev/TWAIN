# EXECUTION LOG v1.0, DANIEL SCHWAMMLEIN
import json
import sqlite3
from datetime import datetime, timezone
DB_NAME = 'executionlog.db'


# Initializes the database, called once
def initDb():
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS jobs(
                submissionId TEXT PRIMARY KEY,
                jobName TEXT NOT NULL,
                user TEXT NOT NULL,
                submitTime TEXT NOT NULL,
                startTime TEXT,
                endTime TEXT,
                status TEXT NOT NULL, -- QUEUED, RUNNING, SUCCESS, FAILURE
                errorMessage TEXT,
                metadata TEXT
                )''')
    con.commit()

# Gets the status of a job from an id
def status(specifiedJobId):
    # Opening database and getting status of specified job
    with sqlite3.connect(DB_NAME) as con:
        cur = con.cursor()
        query = '''SELECT status FROM jobs WHERE submissionId = ?'''
        cur.execute(query, (specifiedJobId,))
        result = cur.fetchone()

        # Returning result
        if result is None:
            return "No job matching specified job id"
        else:
            return result[0]

# New job added to SQL database, should be called whenever the job is queued
def jobQueued(submissionId):
    # Load file data
    with open(f'Logs/Task_Logs/{submissionId}.json', 'r') as file: # submissionId is also the name of the file containing data
        data = json.load(file)
        _submissionId = data["submissionId"]
        _user = data["user"]
        _submitTime = data["submitTime"]
        _jobName = data["jobName"]
        _metadata = json.dumps(data.get("metadata",{}))

    # Add it to the sql database
    with sqlite3.connect(DB_NAME) as con:
        cur = con.cursor()
        cur.execute('''INSERT INTO jobs(submissionId, user, jobName, submitTime, metadata, status) VALUES(?,?,?,?,?,?)''',
                    (_submissionId, _user, _jobName, _submitTime, _metadata, "QUEUED"))
        con.commit()

# Update job's status to RUNNING and startTime to present
def jobStarted(submissionId):
    with sqlite3.connect(DB_NAME) as con:
        cur = con.cursor()
        cur.execute('''UPDATE jobs SET startTime = ?, status = 'RUNNING' WHERE submissionId = ?''',(datetime.now(timezone.utc).isoformat(),submissionId,))
        con.commit()

# Update job's status to SUCCESS and endTime to present
def jobCompleted(submissionId):
    with sqlite3.connect(DB_NAME) as con:
        cur = con.cursor()
        cur.execute('''UPDATE jobs SET endTime = ?, status = 'SUCCESS' WHERE submissionId = ?''',(datetime.now(timezone.utc).isoformat(),submissionId,))
        con.commit()

# Update job's status to FAILURE and add errorMessage
def jobFailed(submissionId, errorMessage):
    with sqlite3.connect(DB_NAME) as con:
        cur = con.cursor()
        cur.execute('''UPDATE jobs SET endTime = ?, errorMessage = ?, status = 'FAILURE' WHERE submissionId = ?''',(datetime.now(timezone.utc).isoformat(), errorMessage, submissionId,))
        con.commit()