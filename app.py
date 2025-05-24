import os
import sqlite3
import secrets
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask import Flask, request, render_template, redirect, url_for, flash, session, g, jsonify, send_from_directory
from functools import wraps

# Configuration - production ready
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'tiff', 'webp'}
DATABASE_PATH = os.environ.get('DATABASE_URL', 'museum.db')
SECRET_KEY = os.environ.get('SECRET_KEY', secrets.token_hex(16))

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
app.secret_key = SECRET_KEY

# Ensure upload directories exist
os.makedirs(os.path.join(UPLOAD_FOLDER, 'artifacts'), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'interpolations'), exist_ok=True)

# Database setup
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        database_url = os.environ.get('DATABASE_URL')
        
        if database_url and 'postgresql' in database_url:
            # Production: PostgreSQL on Railway
            import psycopg2
            import psycopg2.extras
            
            # Fix Railway's postgres:// URL format
            if database_url.startswith('postgres://'):
                database_url = database_url.replace('postgres://', 'postgresql://', 1)
            
            db = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
            g._database = db
        else:
            # Local development: SQLite
            db = g._database = sqlite3.connect('museum.db')
            db.row_factory = sqlite3.Row
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
        
        database_url = os.environ.get('DATABASE_URL', '')
        is_postgres = 'postgresql' in database_url
        
        if is_postgres:
            # PostgreSQL syntax
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS artifacts (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT,
                culture TEXT,
                period TEXT,
                medium TEXT,
                museum TEXT,
                description TEXT,
                image_path TEXT NOT NULL,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT
            )
            ''')
            
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS interpolations (
                id SERIAL PRIMARY KEY,
                model TEXT,
                description TEXT,
                image_path TEXT NOT NULL,
                artifact_ids TEXT NOT NULL,
                weights TEXT NOT NULL,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
            ''')
            
            # Check for existing admin (PostgreSQL)
            cursor.execute("SELECT COUNT(*) as count FROM admins")
            admin_count = cursor.fetchone()['count']
            
        else:
            # SQLite syntax (local development)
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
                image_path TEXT NOT NULL,
                upload_date TEXT DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT
            )
            ''')
            
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS interpolations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT,
                description TEXT,
                image_path TEXT NOT NULL,
                artifact_ids TEXT NOT NULL,
                weights TEXT NOT NULL,
                upload_date TEXT DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
            ''')
            
            # Check for existing admin (SQLite)
            cursor.execute("SELECT COUNT(*) FROM admins")
            admin_count = cursor.fetchone()[0]
        
        # Create default admin if none exists
        if admin_count == 0:
            admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
            if is_postgres:
                cursor.execute(
                    "INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
                    ("admin", generate_password_hash(admin_password))
                )
            else:
                cursor.execute(
                    "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                    ("admin", generate_password_hash(admin_password))
                )
            print("Created default admin user")
            
        db.commit()

# Initialize the database
init_db()

# Helper functions
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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
    artifacts = db.execute("SELECT * FROM artifacts ORDER BY id DESC").fetchall()
    interpolations = db.execute("SELECT * FROM interpolations ORDER BY id DESC").fetchall()
    
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
        artifact1 = db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact1_id,)).fetchone()
        artifact2 = db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact2_id,)).fetchone()
        
        if artifact1 and artifact2:
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
        admin = db.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        
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
        if 'image' not in request.files:
            flash('No image file', 'error')
            return redirect(request.url)
        
        image_file = request.files['image']
        if image_file.filename == '':
            flash('No image selected', 'error')
            return redirect(request.url)
        
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
                return redirect(request.url)
            
            # Insert into database
            db = get_db()
            db.execute(
                """INSERT INTO artifacts 
                   (title, artist, culture, period, medium, museum, description, image_path, metadata) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (title, artist or None, culture or None, period or None, 
                 medium or None, museum or None, description or None, 
                 file_path, metadata or None)
            )
            db.commit()
            
            flash('Artifact uploaded successfully', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid file type. Please upload an image file.', 'error')
    
    return render_template('upload_artifact.html')

@app.route('/upload/interpolation', methods=['GET', 'POST'])
@admin_required
def upload_interpolation():
    db = get_db()
    artifacts = db.execute("SELECT id, title, artist FROM artifacts ORDER BY title").fetchall()
    
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
            
            # Insert into database
            db.execute(
                """INSERT INTO interpolations 
                   (model, description, image_path, artifact_ids, weights) 
                   VALUES (?, ?, ?, ?, ?)""",
                (model, description, file_path, artifact_ids_str, weights_str)
            )
            db.commit()
            
            flash('Interpolation uploaded successfully', 'success')
            return redirect(url_for('index'))
    
    return render_template('upload_interpolation.html', artifacts=artifacts)

@app.route('/view/artifact/<int:artifact_id>')
def view_artifact(artifact_id):
    db = get_db()
    artifact = db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    
    if not artifact:
        flash('Artifact not found', 'error')
        return redirect(url_for('index'))
    
    # Find interpolations using this artifact
    interpolations = []
    all_interpolations = db.execute("SELECT * FROM interpolations").fetchall()
    
    for interp in all_interpolations:
        artifact_ids = interp['artifact_ids'].split(',')
        if str(artifact_id) in artifact_ids:
            interpolations.append(interp)
    
    return render_template('view_artifact.html', artifact=artifact, interpolations=interpolations)

@app.route('/edit/artifact/<int:artifact_id>', methods=['POST'])
@admin_required
def edit_artifact(artifact_id):
    db = get_db()
    
    # Verify artifact exists
    artifact = db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
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
        db.execute(
            """UPDATE artifacts SET 
               title = ?, artist = ?, culture = ?, period = ?, 
               medium = ?, museum = ?, description = ?, metadata = ?
               WHERE id = ?""",
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
    
    # Get artifact info for cleanup
    artifact = db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if not artifact:
        flash('Artifact not found', 'error')
        return redirect(url_for('index'))
    
    try:
        # Check if artifact is used in any interpolations
        interpolations = db.execute(
            "SELECT id FROM interpolations WHERE artifact_ids LIKE ?", 
            (f'%{artifact_id}%',)
        ).fetchall()
        
        if interpolations:
            flash(f'Cannot delete artifact - it is used in {len(interpolations)} interpolation(s). Delete those first.', 'error')
            return redirect(url_for('view_artifact', artifact_id=artifact_id))
        
        # Delete the artifact from database
        db.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
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
    interpolation = db.execute("SELECT * FROM interpolations WHERE id = ?", (interpolation_id,)).fetchone()
    
    if not interpolation:
        flash('Interpolation not found', 'error')
        return redirect(url_for('index'))
    
    # Get source artifacts
    artifact_ids = interpolation['artifact_ids'].split(',')
    weights = interpolation['weights'].split(',')
    
    source_artifacts = []
    for i, artifact_id in enumerate(artifact_ids):
        artifact = db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if artifact:
            artifact_dict = dict(artifact)
            artifact_dict['weight'] = weights[i] if i < len(weights) else "1.0"
            source_artifacts.append(artifact_dict)
    
    return render_template('view_interpolation.html', interpolation=interpolation, source_artifacts=source_artifacts)

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
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)