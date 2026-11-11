import os
import sys

# Import your Flask app instance and the sync function
# (Nuitka will correctly link these during compilation)
from database_setup import app
from routes_client_updater import sync_seed_data

def find_seed_file():
    """
    Bulletproof path resolution to find update_seed.json 
    whether running as standard Python or a compiled Nuitka web app.
    """
    filename = 'update_seed.json'
    
    # 1. Check directory of the executable (Nuitka compiled binary)
    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    
    # 2. Check directory of the current script (Standard Python)
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        script_dir = exe_dir # Fallback if __file__ is stripped by Nuitka
        
    # 3. Check Current Working Directory (Terminal execution path)
    cwd = os.getcwd()
    
    # Prioritized list of paths to check
    possible_paths = [
        os.path.join(cwd, filename),
        os.path.join(exe_dir, filename),
        os.path.join(script_dir, filename)
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            return path
            
    return None

def run_template_installation():
    """
    Safely triggers the template synchronization inside the Flask application context.
    """
    print("==================================================")
    print("🚀 Starting Template Installation Process...")
    print("==================================================")
    
    seed_file_path = find_seed_file()
    
    if not seed_file_path:
        print(f"❌ ERROR: Could not find 'update_seed.json'.")
        print("Please ensure it is uploaded to the same folder as the application.")
        sys.exit(1)
        
    print(f"✅ Found seed file at: {seed_file_path}")
    print("⏳ Loading database context and importing templates...")
    
    # Push the Flask application context so database connections work
    with app.app_context():
        try:
            sync_seed_data()
            print("\n✅ SUCCESS: All templates have been successfully imported into the database!")
        except Exception as e:
            print(f"\n❌ CRITICAL ERROR during import: {str(e)}")
            sys.exit(1)

if __name__ == "__main__":
    run_template_installation()