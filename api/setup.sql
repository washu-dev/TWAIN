-- Create greetings table
CREATE TABLE IF NOT EXISTS greetings (
    id SERIAL PRIMARY KEY,
    message VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert sample greetings
INSERT INTO greetings (message) VALUES
    ('Hello, TWAIN!'),
    ('Welcome to the API'),
    ('Greetings from FastAPI'),
    ('Database connection successful!');

-- Verify table and data
SELECT * FROM greetings;
