# PROVENANCE LOG v1.0, DANIEL SCHWAMMLEIN

import sqlite3
import json
from datetime import datetime, timezone

DB_NAME = 'provenance.db'

# Initializing databse
def initProvenance():
    with sqlite3.connect(DB_NAME) as con:
        cur = con.cursor()

        # Creating the database
        cur.execute('''CREATE TABLE provenance(
        provenanceId INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        submissionId INTEGER NOT NULL, -- corresponds to submissionId for execution log
        user VARCHAR NOT NULL,
        assumptions TEXT,
        decisions TEXT,
        timestamp TEXT
        

        ''')
    return

# Data is given to the provenance log as a python dictionary, dumped from a json file by provenance capture service
# Returns reference id for provenance log
def logProvenance(data):
    with sqlite3.connect(DB_NAME) as con:
        cur = con.cursor()

        # Load data
        _submissionId = data["submissionId"]
        _user = data["user"]
        _assumptions = data["assumptions"]
        _decisions = data["decisions"]

        # Add data to SQL database
        cur.execute("INSERT INTO provenance(submissionId,user,assumptions,decisions) VALUES (?,?,?,?)",
                    (_submissionId, _user, _assumptions, _decisions))
        con.commit()
        return cur.lastrowid



