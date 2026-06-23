from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from database import query_greetings

app = FastAPI(title="TWAIN API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8081",
        "http://localhost:3000",
        "http://localhost:3002",
        "https://d1z5umg4xc2bl8.cloudfront.net",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/api/greetings")
async def get_greetings():
    """Fetch all greetings from the database and return as JSON."""
    try:
        greetings = query_greetings()
        if not greetings:
            return JSONResponse(
                status_code=200,
                content={"data": [], "message": "No greetings found"},
            )
        return JSONResponse(
            status_code=200,
            content={
                "data": greetings,
                "count": len(greetings),
                "message": "Greetings retrieved successfully",
            },
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "message": "Failed to retrieve greetings"},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)  # noqa: S104
