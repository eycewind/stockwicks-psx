#!/bin/bash
echo "🚀 Starting parallel optimization..."

# Get the current directory
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "📁 Running from: $CURRENT_DIR"

# Function for grid search
run_grid_search() {
    echo "🧪 Starting Grid Search Optimizer..."
    python3 "$CURRENT_DIR/threshold_optimizer.py" \
        --symbols-file "$CURRENT_DIR/symbols.txt" \
        --interval 5min \
        --max-combinations 12 \
        --output-dir "$CURRENT_DIR/grid_search_results"
    echo "✅ Grid Search Complete"
}

# Function for quick test  
run_quick_test() {
    echo "⚡ Starting Quick Test..."
    python3 "$CURRENT_DIR/quick_test.py"
    echo "✅ Quick Test Complete"
}

# Create output directory
mkdir -p "$CURRENT_DIR/grid_search_results"

# Run both in parallel
run_grid_search &
run_quick_test &

# Wait for both to complete
wait

echo "🎉 Both optimizations finished!"
echo "📊 Check results in: $CURRENT_DIR/grid_search_results/ and quick_test_*.csv files"