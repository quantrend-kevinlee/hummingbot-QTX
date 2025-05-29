#!/usr/bin/env python3
"""
QTX Perpetual Test Runner

This script provides a convenient way to run QTX perpetual tests with various options:
- Run all tests in the qtx_perpetual folder
- Run specific test segments (udp, api, shm, edge_cases)
- Run individual test files
- Support for unittest discovery and pytest

Usage:
    python run_qtx_tests.py                    # Run all QTX perpetual tests
    python run_qtx_tests.py udp                # Run all UDP-related tests
    python run_qtx_tests.py api                # Run API order book tests
    python run_qtx_tests.py shm                # Run shared memory tests
    python run_qtx_tests.py edge_cases         # Run edge case tests
    python run_qtx_tests.py --file <filename>  # Run specific test file
    python run_qtx_tests.py --list             # List available test segments
    python run_qtx_tests.py --verbose          # Run with verbose output
    python run_qtx_tests.py --pytest          # Use pytest instead of unittest
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional


class QTXTestRunner:
    """Test runner for QTX perpetual connector tests"""
    
    def __init__(self):
        # Since script is now in qtx_helpers/, project root is parent directory
        self.project_root = Path(__file__).parent.parent.absolute()
        self.test_base_path = "test.hummingbot.connector.derivative.qtx_perpetual"
        self.test_dir = self.project_root / "test" / "hummingbot" / "connector" / "derivative" / "qtx_perpetual"
        
        # Define test segments and their corresponding files
        self.test_segments: Dict[str, List[str]] = {
            "udp": [
                "test_qtx_perpetual_udp_manager",
                "test_qtx_perpetual_udp_manager_edge_cases"
            ],
            "api": [
                "test_qtx_perpetual_api_order_book_data_source"
            ],
            "shm": [
                "test_qtx_perpetual_shm_manager"
            ],
            "edge_cases": [
                "test_qtx_perpetual_udp_manager_edge_cases"
            ]
        }
        
        # All test files
        self.all_test_files = [
            "test_qtx_perpetual_udp_manager",
            "test_qtx_perpetual_udp_manager_edge_cases", 
            "test_qtx_perpetual_api_order_book_data_source",
            "test_qtx_perpetual_shm_manager"
        ]
    
    def validate_environment(self) -> bool:
        """Validate that we're in the correct environment"""
        if not self.test_dir.exists():
            print(f"❌ Error: Test directory not found: {self.test_dir}")
            return False
        
        # Check if we're in the project root
        if not (self.project_root / "hummingbot").exists():
            print(f"❌ Error: Not in project root. Expected to find 'hummingbot' directory.")
            return False
        
        return True
    
    def list_available_segments(self) -> None:
        """List all available test segments"""
        print("Available test segments:")
        print("=" * 50)
        
        for segment, files in self.test_segments.items():
            print(f"  {segment:12} - {', '.join(files)}")
        
        print(f"\n  all          - Run all QTX perpetual tests")
        print(f"  --file <name> - Run specific test file")
        
        print(f"\nAvailable test files:")
        for file in self.all_test_files:
            print(f"  - {file}")
    
    def get_test_modules(self, segment: Optional[str] = None, specific_file: Optional[str] = None) -> List[str]:
        """Get list of test modules to run"""
        if specific_file:
            # Remove .py extension if provided
            file_name = specific_file.replace('.py', '')
            if file_name in self.all_test_files:
                return [f"{self.test_base_path}.{file_name}"]
            else:
                print(f"❌ Error: Test file '{file_name}' not found.")
                print(f"Available files: {', '.join(self.all_test_files)}")
                return []
        
        if segment and segment in self.test_segments:
            return [f"{self.test_base_path}.{file}" for file in self.test_segments[segment]]
        
        if segment == "all" or segment is None:
            return [f"{self.test_base_path}.{file}" for file in self.all_test_files]
        
        print(f"❌ Error: Unknown segment '{segment}'")
        print(f"Available segments: {', '.join(self.test_segments.keys())}, all")
        return []
    
    def run_with_unittest(self, modules: List[str], verbose: bool = False) -> int:
        """Run tests using unittest"""
        cmd = [sys.executable, "-m", "unittest"]
        
        if verbose:
            cmd.append("-v")
        
        cmd.extend(modules)
        
        print(f"Running command: {' '.join(cmd)}")
        print("=" * 80)
        
        try:
            result = subprocess.run(cmd, cwd=self.project_root)
            return result.returncode
        except KeyboardInterrupt:
            print("\n❌ Tests interrupted by user")
            return 1
        except Exception as e:
            print(f"❌ Error running tests: {e}")
            return 1
    
    def run_with_pytest(self, modules: List[str], verbose: bool = False) -> int:
        """Run tests using pytest"""
        # Convert module names to file paths for pytest
        test_files = []
        for module in modules:
            # Convert module path to file path
            file_path = module.replace(f"{self.test_base_path}.", "").replace(".", "/")
            full_path = self.test_dir / f"{file_path}.py"
            if full_path.exists():
                test_files.append(str(full_path))
        
        if not test_files:
            print("❌ No valid test files found for pytest")
            return 1
        
        cmd = [sys.executable, "-m", "pytest"]
        
        if verbose:
            cmd.extend(["-v", "-s"])
        
        cmd.extend(test_files)
        
        print(f"Running command: {' '.join(cmd)}")
        print("=" * 80)
        
        try:
            result = subprocess.run(cmd, cwd=self.project_root)
            return result.returncode
        except KeyboardInterrupt:
            print("\n❌ Tests interrupted by user")
            return 1
        except Exception as e:
            print(f"❌ Error running tests: {e}")
            return 1
    
    def run_tests(self, segment: Optional[str] = None, specific_file: Optional[str] = None, 
                  verbose: bool = False, use_pytest: bool = False) -> int:
        """Main method to run tests"""
        if not self.validate_environment():
            return 1
        
        modules = self.get_test_modules(segment, specific_file)
        if not modules:
            return 1
        
        print(f"🚀 Running QTX Perpetual Tests")
        print(f"Test modules: {', '.join(modules)}")
        print(f"Runner: {'pytest' if use_pytest else 'unittest'}")
        print(f"Verbose: {verbose}")
        print("=" * 80)
        
        if use_pytest:
            return self.run_with_pytest(modules, verbose)
        else:
            return self.run_with_unittest(modules, verbose)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="QTX Perpetual Test Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_qtx_tests.py                    # Run all tests
  python run_qtx_tests.py udp                # Run UDP tests
  python run_qtx_tests.py api                # Run API tests
  python run_qtx_tests.py shm                # Run shared memory tests
  python run_qtx_tests.py edge_cases         # Run edge case tests
  python run_qtx_tests.py --file test_qtx_perpetual_udp_manager
  python run_qtx_tests.py --list             # List available segments
  python run_qtx_tests.py --verbose udp      # Run UDP tests with verbose output
  python run_qtx_tests.py --pytest api      # Run API tests with pytest
        """
    )
    
    parser.add_argument(
        "segment", 
        nargs="?", 
        help="Test segment to run (udp, api, shm, edge_cases, all)"
    )
    
    parser.add_argument(
        "--file", 
        dest="specific_file",
        help="Run specific test file"
    )
    
    parser.add_argument(
        "--list", 
        action="store_true",
        help="List available test segments and files"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Run tests with verbose output"
    )
    
    parser.add_argument(
        "--pytest",
        action="store_true", 
        help="Use pytest instead of unittest"
    )
    
    args = parser.parse_args()
    
    runner = QTXTestRunner()
    
    if args.list:
        runner.list_available_segments()
        return 0
    
    # Default to "all" if no segment specified and no specific file
    segment = args.segment if args.segment else ("all" if not args.specific_file else None)
    
    return runner.run_tests(
        segment=segment,
        specific_file=args.specific_file,
        verbose=args.verbose,
        use_pytest=args.pytest
    )


if __name__ == "__main__":
    sys.exit(main()) 