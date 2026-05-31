#!/bin/bash
# M1 Beat Plan prototype - one-command launcher
echo "Installing dependencies (first run only)..."
pip install -q flask pandas openpyxl scikit-learn folium ortools 2>/dev/null
echo "Starting app at http://localhost:5000  (Ctrl+C to stop)"
python3 app.py
