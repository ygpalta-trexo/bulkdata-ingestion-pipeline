#!/usr/bin/env python3
"""
Quick test script to demonstrate process_folder.py usage.

This script shows how to:
1. Extract a sample ZIP file
2. Process it with process_folder.py
3. View the results

Usage:
    python test_process_folder.py
"""

import os
import subprocess
import sys
from pathlib import Path

def run_command(cmd, description):
    """Run a command and return success status."""
    print(f"\n{'='*50}")
    print(f"Running: {description}")
    print(f"Command: {' '.join(cmd)}")
    print('='*50)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).parent)
        if result.returncode == 0:
            print("✅ SUCCESS")
            if result.stdout:
                # Show last few lines of output
                lines = result.stdout.strip().split('\n')
                for line in lines[-10:]:
                    if line.strip():
                        print(f"  {line}")
        else:
            print("❌ FAILED")
            if result.stderr:
                print(f"Error: {result.stderr}")
        return result.returncode == 0
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return False

def main():
    print("Testing process_folder.py script")
    print("This will demonstrate processing ZIP files from a folder")

    # Check if we have the required files
    script_path = Path("process_folder.py")
    if not script_path.exists():
        print("❌ process_folder.py not found!")
        return 1

    # Check if tmp_downloads exists and has ZIP files
    tmp_downloads = Path("tmp_downloads")
    if not tmp_downloads.exists():
        print("❌ tmp_downloads folder not found!")
        print("Please run the main pipeline first to download some files")
        return 1

    zip_files = list(tmp_downloads.glob("**/*.zip"))
    if not zip_files:
        print("❌ No ZIP files found in tmp_downloads!")
        print("Please run the main pipeline first to download some files")
        return 1

    print(f"Found {len(zip_files)} ZIP files in tmp_downloads")

    # Test 1: Dry run with limit
    success1 = run_command([
        sys.executable, "process_folder.py", "tmp_downloads",
        "--dry-run", "--limit", "1", "--verbose"
    ], "Dry run processing 1 ZIP file")

    # Test 2: Save results to JSON
    success2 = run_command([
        sys.executable, "process_folder.py", "tmp_downloads",
        "--dry-run", "--limit", "1", "--output-json", "test_results.json"
    ], "Process 1 ZIP file and save results to JSON")

    # Check if JSON file was created
    json_file = Path("test_results.json")
    if json_file.exists():
        print(f"✅ JSON results file created: {json_file}")
        size = json_file.stat().st_size
        print(f"   File size: {size} bytes")
        # Clean up
        json_file.unlink()
        print("   Cleaned up test file")
    else:
        print("❌ JSON results file was not created")

    # Test 3: Show help
    success3 = run_command([
        sys.executable, "process_folder.py", "--help"
    ], "Show help information")

    print(f"\n{'='*50}")
    print("TEST SUMMARY")
    print('='*50)
    print(f"Dry run test: {'✅ PASS' if success1 else '❌ FAIL'}")
    print(f"JSON output test: {'✅ PASS' if success2 else '❌ FAIL'}")
    print(f"Help display test: {'✅ PASS' if success3 else '❌ FAIL'}")

    if success1 and success2 and success3:
        print("\n🎉 All tests passed! process_folder.py is working correctly.")
        print("\nUsage examples:")
        print("  # Process all ZIP files in a folder (dry run)")
        print("  python process_folder.py /path/to/zips --dry-run")
        print("")
        print("  # Process and save to database")
        print("  python process_folder.py /path/to/zips --dsn 'postgresql://...'")
        print("")
        print("  # Process with results saved to JSON")
        print("  python process_folder.py /path/to/zips --output-json results.json")
        print("")
        print("  # Process only first 5 files")
        print("  python process_folder.py /path/to/zips --limit 5")
        return 0
    else:
        print("\n❌ Some tests failed. Please check the output above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())