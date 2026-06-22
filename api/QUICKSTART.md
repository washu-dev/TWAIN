# Quick Start Guide - TWAIN API

## Step 1: Install Dependencies
```bash
cd api
pip install -r requirements.txt
```

## Step 2: Configure Database
Edit `.env` with your PostgreSQL credentials:
```bash
nano .env
```

Expected format:
```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=twain_db
DB_USER=postgres
DB_PASSWORD=your_password_here
```

## Step 3: Create Database Table
Run the SQL setup script:
```bash
psql -U postgres -h localhost -d twain_db -f setup.sql
```

Or manually:
```bash
psql -U postgres -h localhost -d twain_db
```

Then paste:
```sql
CREATE TABLE IF NOT EXISTS greetings (
    id SERIAL PRIMARY KEY,
    message VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO greetings (message) VALUES
    ('Hello, TWAIN!'),
    ('Welcome to the API'),
    ('Greetings from FastAPI'),
    ('Database connection successful!');
```

## Step 4: Run the API
```bash
python main.py
```

API will be available at: **http://localhost:8000**

## Step 5: Test the API

### Using curl:
```bash
# Health check
curl http://localhost:8000/api/health

# Get greetings
curl http://localhost:8000/api/greetings
```

### Using Python:
```bash
python -m pytest test_api.py -v
```

### Interactive API Docs:
Open in browser: **http://localhost:8000/docs**

## Troubleshooting

### Database Connection Error
- Verify PostgreSQL is running: `psql -U postgres`
- Check .env credentials match your setup
- Ensure twain_db database exists: `createdb -U postgres twain_db`

### Port 8000 Already in Use
Change port in `main.py`:
```python
uvicorn.run(app, host="0.0.0.0", port=8001, reload=True)
```

### Missing Dependencies
Reinstall requirements:
```bash
pip install -r requirements.txt --force-reinstall
```

## Files Overview

| File | Purpose |
|------|---------|
| `main.py` | FastAPI application & endpoints |
| `database.py` | PostgreSQL connection logic |
| `.env` | Database credentials (add to .gitignore) |
| `requirements.txt` | Python dependencies |
| `test_api.py` | Test suite (pytest) |
| `setup.sql` | Database table creation script |
| `README.md` | Full documentation |
