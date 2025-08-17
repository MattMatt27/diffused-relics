import os
import secrets
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask import Flask, request, render_template, redirect, url_for, flash, session, g, jsonify, send_from_directory
from functools import wraps
from harvard_api import get_api_client, get_display_image_url, get_thumbnail_url

# Configuration - production ready
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'tiff', 'webp'}
DATABASE_URL = os.environ.get('DATABASE_URL', '')
SECRET_KEY = os.environ.get('SECRET_KEY', secrets.token_hex(16))

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
app.secret_key = SECRET_KEY

# Ensure upload directories exist
os.makedirs(os.path.join(UPLOAD_FOLDER, 'artifacts'), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'interpolations'), exist_ok=True)

# Database setup - PostgreSQL only
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        import psycopg2
        import psycopg2.extras
        
        # Fix Railway's postgres:// URL format
        database_url = DATABASE_URL
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        
        db = g._database = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        
        # PostgreSQL syntax
        primary_key = "SERIAL PRIMARY KEY"
        timestamp_default = "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        unique_constraint = "UNIQUE"
        
        # Create artifacts table with Harvard API fields
        cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS artifacts (
            id {primary_key},
            title TEXT NOT NULL,
            artist TEXT,
            culture TEXT,
            period TEXT,
            medium TEXT,
            museum TEXT,
            description TEXT,
            image_path TEXT,
            upload_date {timestamp_default},
            metadata TEXT,
            -- Harvard API fields
            harvard_object_id INTEGER {unique_constraint},
            harvard_object_number TEXT,
            classification TEXT,
            dated TEXT,
            date_begin INTEGER,
            date_end INTEGER,
            century TEXT,
            technique TEXT,
            dimensions TEXT,
            provenance TEXT,
            creditline TEXT,
            department TEXT,
            division TEXT,
            copyright TEXT,
            verification_level INTEGER,
            image_permission_level INTEGER DEFAULT 0,
            access_level INTEGER DEFAULT 1,
            harvard_url TEXT,
            primary_image_url TEXT,
            iiif_base_uri TEXT,
            last_api_sync {timestamp_default.replace('CURRENT_TIMESTAMP', 'NULL')},
            section TEXT
        )
        ''')
        
        # Create interpolations table
        cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS interpolations (
            id {primary_key},
            model TEXT,
            description TEXT,
            image_path TEXT NOT NULL,
            artifact_ids TEXT NOT NULL,
            weights TEXT NOT NULL,
            upload_date {timestamp_default}
        )
        ''')
        
        # Create admin users table
        cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS admins (
            id {primary_key},
            username TEXT {unique_constraint} NOT NULL,
            password_hash TEXT NOT NULL
        )
        ''')
        
        # Create a default admin if none exists
        cursor.execute("SELECT COUNT(*) as count FROM admins")
        admin_count = cursor.fetchone()['count']
        
        if admin_count == 0:
            admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
            cursor.execute(
                "INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
                ("admin", generate_password_hash(admin_password))
            )
            print("Created default admin user")
            
        db.commit()

# Initialize the database
init_db()

# Helper functions
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def is_local_testing_with_remote_db():
    """Check if we're running locally in development mode"""
    # In development mode, we use local preview database for manual uploads
    return os.environ.get('FLASK_ENV') == 'development'

def get_local_preview_db():
    """Get connection to local preview database for testing manual uploads"""
    import sqlite3
    preview_db_path = 'local_preview.db'
    
    try:
        conn = sqlite3.connect(preview_db_path)
        conn.row_factory = sqlite3.Row
        
        # Create artifacts table in preview database if it doesn't exist
        cursor = conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            artist TEXT,
            culture TEXT,
            period TEXT,
            medium TEXT,
            museum TEXT,
            description TEXT,
            image_path TEXT,
            upload_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT,
            is_local_preview INTEGER DEFAULT 1
        )
        ''')
        
        # Create interpolations table in preview database
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS interpolations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT,
            description TEXT,
            image_path TEXT NOT NULL,
            artifact_ids TEXT NOT NULL,
            weights TEXT NOT NULL,
            upload_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_local_preview INTEGER DEFAULT 1
        )
        ''')
        
        conn.commit()
        return conn
    except Exception as e:
        print(f"Error creating local preview database: {e}")
        return None

# Authentication decorator
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            flash('Please log in to access this page', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Routes
@app.route('/')
def index():
    db = get_db()
    cursor = db.cursor()
    
    # Get main database artifacts
    cursor.execute("SELECT * FROM artifacts ORDER BY id DESC")
    artifacts = list(cursor.fetchall())
    
    # Add display image URLs for each artifact
    for artifact in artifacts:
        artifact['display_image_url'] = get_display_image_url(dict(artifact))
        artifact['thumbnail_url'] = get_thumbnail_url(dict(artifact))
        artifact['is_local_preview'] = False
    
    cursor.execute("SELECT * FROM interpolations ORDER BY id DESC")
    interpolations = list(cursor.fetchall())
    
    # Add local preview items when testing with remote database
    if is_local_testing_with_remote_db():
        preview_db = get_local_preview_db()
        if preview_db:
            preview_cursor = preview_db.cursor()
            
            # Add preview artifacts
            preview_cursor.execute("SELECT * FROM artifacts ORDER BY id DESC")
            preview_artifacts = preview_cursor.fetchall()
            for artifact in preview_artifacts:
                artifact_dict = dict(artifact)
                artifact_dict['display_image_url'] = f"/static/uploads/{artifact['image_path']}"
                artifact_dict['thumbnail_url'] = f"/static/uploads/{artifact['image_path']}"
                artifact_dict['is_local_preview'] = True
                artifacts.insert(0, artifact_dict)  # Add to top of list
            
            # Add preview interpolations
            preview_cursor.execute("SELECT * FROM interpolations ORDER BY id DESC")
            preview_interpolations = preview_cursor.fetchall()
            for interp in preview_interpolations:
                interp_dict = dict(interp)
                interp_dict['is_local_preview'] = True
                interpolations.insert(0, interp_dict)
                
            preview_db.close()
    
    # Create data structure for the artifact pairs and their interpolations
    paired_interpolations = []
    
    # Get all interpolations with exactly 2 source artifacts
    two_artifact_interpolations = []
    for interp in interpolations:
        artifact_ids = interp['artifact_ids'].split(',')
        weights = interp['weights'].split(',')
        
        if len(artifact_ids) == 2:
            two_artifact_interpolations.append({
                'id': interp['id'],
                'image_path': interp['image_path'],
                'model': interp['model'],
                'artifact_id1': int(artifact_ids[0]),
                'artifact_id2': int(artifact_ids[1]),
                'weight1': float(weights[0]),
                'weight2': float(weights[1])
            })
    
    # Group interpolations by artifact pairs
    pairs = {}
    for interp in two_artifact_interpolations:
        # Create a consistent key for the pair (always smaller ID first)
        pair_key = (min(interp['artifact_id1'], interp['artifact_id2']), 
                    max(interp['artifact_id1'], interp['artifact_id2']))
        
        if pair_key not in pairs:
            pairs[pair_key] = []
        
        # Add interpolation to the pair
        pairs[pair_key].append(interp)
    
    # Format the data for the template
    for pair_key, interps in pairs.items():
        artifact1_id, artifact2_id = pair_key
        
        # Get artifact details
        cursor.execute("SELECT * FROM artifacts WHERE id = %s", (artifact1_id,))
        artifact1 = cursor.fetchone()
        
        cursor.execute("SELECT * FROM artifacts WHERE id = %s", (artifact2_id,))
        artifact2 = cursor.fetchone()
        
        if artifact1 and artifact2:
            # Add display URLs for artifacts
            artifact1['display_image_url'] = get_display_image_url(dict(artifact1))
            artifact2['display_image_url'] = get_display_image_url(dict(artifact2))
            
            # Calculate position for each interpolation
            for interp in interps:
                # Normalize the weights to get position
                total_weight = interp['weight1'] + interp['weight2']
                
                # Check which artifact is which (they might be reversed in the pair_key)
                if interp['artifact_id1'] == artifact1_id:
                    # Position is percentage from left (0%) to right (100%)
                    interp['position'] = (interp['weight2'] / total_weight) * 100
                else:
                    # Swap the weights if the artifacts are swapped
                    interp['weight1'], interp['weight2'] = interp['weight2'], interp['weight1']
                    interp['position'] = (interp['weight2'] / total_weight) * 100
            
            # Sort interpolations by position (left to right)
            interps.sort(key=lambda x: x['position'])
            
            # Add to paired_interpolations
            paired_interpolations.append({
                'artifact1': artifact1,
                'artifact2': artifact2,
                'interpolations': interps
            })
    
    return render_template('index.html', 
                           artifacts=artifacts, 
                           interpolations=interpolations,
                           paired_interpolations=paired_interpolations)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM admins WHERE username = %s", (username,))
        admin = cursor.fetchone()
        
        if admin and check_password_hash(admin['password_hash'], password):
            session['admin_id'] = admin['id']
            session['username'] = admin['username']
            flash('Login successful', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('admin_id', None)
    session.pop('username', None)
    flash('You have been logged out', 'info')
    return redirect(url_for('index'))

@app.route('/upload/artifact', methods=['GET', 'POST'])
@admin_required
def upload_artifact():
    if request.method == 'POST':
        # Check if this is a Harvard API artifact or manual upload
        harvard_id = request.form.get('harvard_id')
        
        if harvard_id:
            # Harvard API artifact
            return add_harvard_artifact(harvard_id)
        else:
            # Manual upload (existing functionality)
            return add_manual_artifact()
    
    return render_template('upload_artifact.html')

def add_harvard_artifact(harvard_id):
    """Add artifact from Harvard API"""
    try:
        harvard_id = int(harvard_id)
        
        # Check if already exists
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT id FROM artifacts WHERE harvard_object_id = %s", (harvard_id,))
        existing = cursor.fetchone()
        
        if existing:
            flash(f'Artifact with Harvard ID {harvard_id} already exists', 'info')
            return redirect(url_for('view_artifact', artifact_id=existing['id']))
        
        # Fetch from API
        api_client = get_api_client()
        if not api_client:
            flash('Harvard API not available. Please check API key.', 'error')
            return redirect(url_for('upload_artifact'))
        
        api_data = api_client.get_object_by_id(harvard_id)
        if not api_data:
            flash(f'Object with ID {harvard_id} not found in Harvard Art Museums', 'error')
            return redirect(url_for('upload_artifact'))
        
        # Extract data for database
        artifact_data = api_client.extract_artifact_data(api_data)
        artifact_data['image_path'] = 'harvard_placeholder.jpg'  # Placeholder for Harvard images
        artifact_data['last_api_sync'] = datetime.now()
        
        # Insert into database
        insert_query = """
        INSERT INTO artifacts (
            harvard_object_id, harvard_object_number, title, artist, culture, period, medium, museum, 
            description, classification, dated, date_begin, date_end, century, technique, dimensions, 
            provenance, creditline, department, division, copyright, verification_level, 
            image_permission_level, access_level, harvard_url, primary_image_url, iiif_base_uri, 
            last_api_sync, image_path
        ) VALUES (
            %(harvard_object_id)s, %(harvard_object_number)s, %(title)s, %(artist)s, %(culture)s, 
            %(period)s, %(medium)s, %(museum)s, %(description)s, %(classification)s, %(dated)s, 
            %(date_begin)s, %(date_end)s, %(century)s, %(technique)s, %(dimensions)s, %(provenance)s, 
            %(creditline)s, %(department)s, %(division)s, %(copyright)s, %(verification_level)s, 
            %(image_permission_level)s, %(access_level)s, %(harvard_url)s, %(primary_image_url)s, 
            %(iiif_base_uri)s, %(last_api_sync)s, %(image_path)s
        ) RETURNING id
        """
        
        cursor.execute(insert_query, artifact_data)
        artifact_id = cursor.fetchone()['id']
        db.commit()
        
        flash(f'Harvard artifact "{artifact_data["title"]}" added successfully!', 'success')
        return redirect(url_for('view_artifact', artifact_id=artifact_id))
        
    except ValueError:
        flash('Invalid Harvard object ID', 'error')
        return redirect(url_for('upload_artifact'))
    except Exception as e:
        flash(f'Error adding Harvard artifact: {str(e)}', 'error')
        return redirect(url_for('upload_artifact'))

def add_manual_artifact():
    """Add artifact via manual upload (existing functionality)"""
    if 'image' not in request.files:
        flash('No image file', 'error')
        return redirect(url_for('upload_artifact'))
    
    image_file = request.files['image']
    if image_file.filename == '':
        flash('No image selected', 'error')
        return redirect(url_for('upload_artifact'))
    
    if image_file and allowed_file(image_file.filename):
        # Save the image
        filename = secure_filename(image_file.filename)
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        file_path = f"artifacts/{timestamp}_{filename}"
        full_path = os.path.join(app.config['UPLOAD_FOLDER'], file_path)
        image_file.save(full_path)
        
        # Get form data
        title = request.form.get('title', '').strip()
        artist = request.form.get('artist', '').strip()
        culture = request.form.get('culture', '').strip()
        period = request.form.get('period', '').strip()
        medium = request.form.get('medium', '').strip()
        museum = request.form.get('museum', '').strip()
        description = request.form.get('description', '').strip()
        metadata = request.form.get('metadata', '').strip()
        
        # Validate required fields
        if not title:
            flash('Title is required', 'error')
            return redirect(url_for('upload_artifact'))
        
        # Choose database based on testing mode
        use_preview_db = is_local_testing_with_remote_db()
        
        if use_preview_db:
            # Use local preview database for testing
            db = get_local_preview_db()
            if not db:
                flash('Error setting up local preview database', 'error')
                return redirect(url_for('upload_artifact'))
            
            cursor = db.cursor()
            cursor.execute(
                """INSERT INTO artifacts 
                   (title, artist, culture, period, medium, museum, description, image_path, metadata) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (title, artist or None, culture or None, period or None, 
                 medium or None, museum or None, description or None, 
                 file_path, metadata or None)
            )
            db.commit()
            db.close()
            
            flash('Artifact uploaded to local preview (not saved to production database)', 'info')
        else:
            # Use main database (production or local SQLite)
            db = get_db()
            cursor = db.cursor()
            cursor.execute(
                """INSERT INTO artifacts 
                   (title, artist, culture, period, medium, museum, description, image_path, metadata) 
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (title, artist or None, culture or None, period or None, 
                 medium or None, museum or None, description or None, 
                 file_path, metadata or None)
            )
            db.commit()
            
            flash('Artifact uploaded successfully', 'success')
        
        return redirect(url_for('index'))
    else:
        flash('Invalid file type. Please upload an image file.', 'error')
        return redirect(url_for('upload_artifact'))

@app.route('/upload/interpolation', methods=['GET', 'POST'])
@admin_required
def upload_interpolation():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, title, artist FROM artifacts ORDER BY title")
    artifacts = cursor.fetchall()
    
    if request.method == 'POST':
        if 'image' not in request.files:
            flash('No image file', 'error')
            return redirect(request.url)
        
        image_file = request.files['image']
        if image_file.filename == '':
            flash('No image selected', 'error')
            return redirect(request.url)
        
        artifact_ids = request.form.getlist('artifact_id')
        weights = request.form.getlist('weight')
        
        if len(artifact_ids) < 2:
            flash('Please select at least 2 source artifacts', 'error')
            return redirect(request.url)
        
        if image_file and allowed_file(image_file.filename):
            # Save the image
            filename = secure_filename(image_file.filename)
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            file_path = f"interpolations/{timestamp}_{filename}"
            full_path = os.path.join(app.config['UPLOAD_FOLDER'], file_path)
            image_file.save(full_path)
            
            # Get form data
            model = request.form.get('model', '')
            description = request.form.get('description', '')
            
            # Format artifact IDs and weights as comma-separated strings
            artifact_ids_str = ','.join(artifact_ids)
            weights_str = ','.join(weights)
            
            # Choose database based on testing mode
            use_preview_db = is_local_testing_with_remote_db()
            
            if use_preview_db:
                # Use local preview database for testing
                preview_db = get_local_preview_db()
                if not preview_db:
                    flash('Error setting up local preview database', 'error')
                    return redirect(request.url)
                
                preview_cursor = preview_db.cursor()
                preview_cursor.execute(
                    """INSERT INTO interpolations 
                       (model, description, image_path, artifact_ids, weights) 
                       VALUES (?, ?, ?, ?, ?)""",
                    (model, description, file_path, artifact_ids_str, weights_str)
                )
                preview_db.commit()
                preview_db.close()
                
                flash('Interpolation uploaded to local preview (not saved to production database)', 'info')
            else:
                # Insert into main database
                cursor.execute(
                    """INSERT INTO interpolations 
                       (model, description, image_path, artifact_ids, weights) 
                       VALUES (%s, %s, %s, %s, %s)""",
                    (model, description, file_path, artifact_ids_str, weights_str)
                )
                db.commit()
                
                flash('Interpolation uploaded successfully', 'success')
                
            return redirect(url_for('index'))
    
    return render_template('upload_interpolation.html', artifacts=artifacts)

@app.route('/view/artifact/<int:artifact_id>')
def view_artifact(artifact_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,))
    artifact = cursor.fetchone()
    
    if not artifact:
        flash('Artifact not found', 'error')
        return redirect(url_for('index'))
    
    # Add display image URL
    artifact_dict = dict(artifact)
    artifact_dict['display_image_url'] = get_display_image_url(artifact_dict)
    artifact_dict['thumbnail_url'] = get_thumbnail_url(artifact_dict)
    
    # Find interpolations using this artifact
    interpolations = []
    
    # Check main database
    cursor.execute("SELECT * FROM interpolations")
    all_interpolations = list(cursor.fetchall())
    
    for interp in all_interpolations:
        artifact_ids = interp['artifact_ids'].split(',')
        if str(artifact_id) in artifact_ids:
            interp_dict = dict(interp)
            interp_dict['is_local_preview'] = False
            interpolations.append(interp_dict)
    
    # Also check local preview database if in development mode
    if is_local_testing_with_remote_db():
        preview_db = get_local_preview_db()
        if preview_db:
            preview_cursor = preview_db.cursor()
            preview_cursor.execute("SELECT * FROM interpolations")
            preview_interpolations = preview_cursor.fetchall()
            
            for interp in preview_interpolations:
                artifact_ids = interp['artifact_ids'].split(',')
                if str(artifact_id) in artifact_ids:
                    interp_dict = dict(interp)
                    interp_dict['is_local_preview'] = True
                    interpolations.append(interp_dict)
                    
            preview_db.close()
    
    return render_template('view_artifact.html', artifact=artifact_dict, interpolations=interpolations)

@app.route('/edit/artifact/<int:artifact_id>', methods=['POST'])
@admin_required
def edit_artifact(artifact_id):
    db = get_db()
    cursor = db.cursor()
    
    # Verify artifact exists
    cursor.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,))
    artifact = cursor.fetchone()
    if not artifact:
        flash('Artifact not found', 'error')
        return redirect(url_for('index'))
    
    # Get form data
    title = request.form.get('title', '').strip()
    artist = request.form.get('artist', '').strip()
    culture = request.form.get('culture', '').strip()
    period = request.form.get('period', '').strip()
    medium = request.form.get('medium', '').strip()
    museum = request.form.get('museum', '').strip()
    description = request.form.get('description', '').strip()
    metadata = request.form.get('metadata', '').strip()
    
    # Validate required fields
    if not title:
        flash('Title is required', 'error')
        return redirect(url_for('view_artifact', artifact_id=artifact_id))
    
    try:
        # Update the artifact
        cursor.execute(
            """UPDATE artifacts SET 
               title = %s, artist = %s, culture = %s, period = %s, 
               medium = %s, museum = %s, description = %s, metadata = %s
               WHERE id = %s""",
            (title, artist or None, culture or None, period or None, 
             medium or None, museum or None, description or None, 
             metadata or None, artifact_id)
        )
        db.commit()
        
        flash('Artifact updated successfully', 'success')
        
    except Exception as e:
        flash(f'Error updating artifact: {str(e)}', 'error')
        db.rollback()
    
    return redirect(url_for('view_artifact', artifact_id=artifact_id))

@app.route('/delete/artifact/<int:artifact_id>')
@admin_required
def delete_artifact(artifact_id):
    db = get_db()
    cursor = db.cursor()
    
    # Get artifact info for cleanup
    cursor.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,))
    artifact = cursor.fetchone()
    if not artifact:
        flash('Artifact not found', 'error')
        return redirect(url_for('index'))
    
    try:
        # Check if artifact is used in any interpolations
        cursor.execute("SELECT id FROM interpolations WHERE artifact_ids LIKE %s", (f'%{artifact_id}%',))
        interpolations = cursor.fetchall()
        
        if interpolations:
            flash(f'Cannot delete artifact - it is used in {len(interpolations)} interpolation(s). Delete those first.', 'error')
            return redirect(url_for('view_artifact', artifact_id=artifact_id))
        
        # Delete the artifact from database
        cursor.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
        db.commit()
        
        # Try to delete the image file
        try:
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], artifact['image_path'])
            if os.path.exists(image_path):
                os.remove(image_path)
        except Exception as e:
            print(f"Warning: Could not delete image file: {e}")
        
        flash(f'Artifact "{artifact["title"]}" deleted successfully', 'success')
        
    except Exception as e:
        flash(f'Error deleting artifact: {str(e)}', 'error')
        db.rollback()
        return redirect(url_for('view_artifact', artifact_id=artifact_id))
    
    return redirect(url_for('index'))

@app.route('/view/interpolation/<int:interpolation_id>')
def view_interpolation(interpolation_id):
    db = get_db()
    cursor = db.cursor()
    
    # First try main database
    cursor.execute("SELECT * FROM interpolations WHERE id = %s", (interpolation_id,))
    interpolation = cursor.fetchone()
    is_preview = False
    
    # If not found and we're in local testing mode, check preview database
    if not interpolation and is_local_testing_with_remote_db():
        preview_db = get_local_preview_db()
        if preview_db:
            preview_cursor = preview_db.cursor()
            preview_cursor.execute("SELECT * FROM interpolations WHERE id = ?", (interpolation_id,))
            interpolation = preview_cursor.fetchone()
            if interpolation:
                is_preview = True
            preview_db.close()
    
    if not interpolation:
        flash('Interpolation not found', 'error')
        return redirect(url_for('index'))
    
    # Get source artifacts
    artifact_ids = interpolation['artifact_ids'].split(',')
    weights = interpolation['weights'].split(',')
    
    source_artifacts = []
    for i, artifact_id in enumerate(artifact_ids):
        # Always check main database for artifacts (since we want real artifact data)
        cursor.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,))
        artifact = cursor.fetchone()
        if artifact:
            artifact_dict = dict(artifact)
            artifact_dict['weight'] = weights[i] if i < len(weights) else "1.0"
            # Add display image URLs
            artifact_dict['display_image_url'] = get_display_image_url(artifact_dict)
            artifact_dict['thumbnail_url'] = get_thumbnail_url(artifact_dict)
            source_artifacts.append(artifact_dict)
    
    # Add preview flag to interpolation
    interpolation_dict = dict(interpolation)
    interpolation_dict['is_local_preview'] = is_preview
    
    # Fix upload_date if it's a string (from SQLite preview database)
    if is_preview and interpolation_dict.get('upload_date') and isinstance(interpolation_dict['upload_date'], str):
        from datetime import datetime
        try:
            # Parse the string datetime from SQLite
            interpolation_dict['upload_date'] = datetime.fromisoformat(interpolation_dict['upload_date'].replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            interpolation_dict['upload_date'] = None
    
    return render_template('view_interpolation.html', interpolation=interpolation_dict, source_artifacts=source_artifacts)

# API endpoints for Harvard search
@app.route('/api/search')
def api_search_harvard():
    """API endpoint for searching Harvard artifacts"""
    query = request.args.get('q', '').strip()
    
    if len(query) < 2:
        return jsonify({'suggestions': []})
    
    try:
        api_client = get_api_client()
        if not api_client:
            return jsonify({'error': 'Harvard API not available'}), 500
        
        results = api_client.search_objects(query, size=5)
        return jsonify({'suggestions': results})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/artifact/<int:harvard_id>')
def api_get_harvard_artifact(harvard_id):
    """Get full Harvard artifact details"""
    try:
        api_client = get_api_client()
        if not api_client:
            return jsonify({'error': 'Harvard API not available'}), 500
        
        data = api_client.get_object_by_id(harvard_id)
        if data:
            return jsonify(data)
        else:
            return jsonify({'error': 'Artifact not found'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Health check endpoint for Railway
@app.route('/health')
def health_check():
    return {'status': 'healthy', 'timestamp': datetime.now().isoformat()}

# Static file serving (Railway handles this automatically)
@app.route('/static/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# Production server configuration
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5005))
    debug_mode = os.environ.get('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)