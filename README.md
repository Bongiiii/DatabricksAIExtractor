# PDF Table Extractor

A React-based web application that uses AI to extract tabular data from dense PDF documents (especially scientific papers) and converts them into clean Excel files. Perfect for when you have old and bulky government documents full of species data and you're big on automation.

## How It Works

- **Upload PDFs**: Drag/drop
- **AI Extraction**: Uses OpenAI's GPT-4 Vision to read tables 
- **Smart Parsing**: Handles dense scientific documents with multiple columns and implicit table structures(well in this case atleast)
- **Preview feature**: See your extracted data before committing to download(see like the first 20 rows)
- **Excel Export**: access the extracted data and downloads it as an Excel file
- **Test Mode**: Process just a few pages first (kinda works if you know theres a table in those few pages, lol)

## Tech Stack

- **Frontend**: React 
- **Backend**: FastAPI + Python 
- **AI**: OpenAI GPT-4 Vision 
- **PDF Processing**: PyMuPDF 
- **Excel Magic**: openpyxl + XLSX 

## Getting Started

### Prerequisites

- Node.js 
- Python 3.8+ 
- OpenAI API key (worth every $$$)
- An old PDF(even new) and a deadline hehe

### Setup (The "Please Work" Dance)

1. **Clone this repo** 

2. **Backend Setup**
   pip install -r requirements.txt
   Don't forget to actually have the openai api key(one that works)

3. **Frontend Setup**
   ```bash
   npm install
   ```

## Running the App

### Start the Backend (The Brain)
```bash
Run either:
python main.py
# OR 
uvicorn main:app --reload
```
Backend will be running on `http://localhost:8000` 
### Start the Frontend 
```bash
# From your frontend directory
npm start
```
Frontend will be running on `http://localhost:3000`

## How to Use

1. **Upload a PDF** - Click the upload area 
2. **Set Your Columns** - Tell it what data you want (e.g., "Species, Location, Status")
3. **Add Instructions** - Any special requests for the AI overlord
4. **Test Mode** - Start with a few pages if you're feeling cautious
5. **Hit Extract** - patience is kinda a prerequisite for this app actually. Bulkier the pdf, the longer the wait period(linear time complexityvibes)
6. **Preview Results** - Make sure it didn't hallucinate
7. **Download or Discard** - decisions, decisions

## Pro Tips

- Start with test mode 
- Be specific with your column names
- Scientific PDFs work best (it's trained on those)

