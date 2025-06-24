#!/usr/bin/env python3

import os
import signal
import sys
import logging
import asyncio
import json
import tomllib
import pysqlite3.dbapi2 as sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import paho.mqtt.client as mqtt
from pyais import decode
from pyais.stream import TCPConnection

background_tasks = set()
stop_event = asyncio.Event()
last_cleanup_times = {}
ais_vessels = {}
mqtt_client = None
websocket_connections = set()
shutdown_complete = asyncio.Event()
_config_cache = None
_config_cache_time = 0

def get_config():
    global _config_cache, _config_cache_time
    import time
    
    current_time = time.time()
    # Cache for 30 seconds
    if _config_cache is None or (current_time - _config_cache_time) > 30:
        try:
            with open('conf/config.toml', 'rb') as f:
                _config_cache = tomllib.load(f)
                _config_cache_time = current_time
        except Exception as e:
            logging.error(f"Config error: {e}")
            raise
    
    return _config_cache

def configure_logging():
    try:
        config = get_config()
        app_mode = config.get('lpu', {}).get('app_mode', 'development')
        
        if app_mode == 'production':
            log_level = logging.WARNING
        else:
            log_level = logging.INFO
            
        # Clear any existing handlers and reconfigure
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
            
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
            force=True
        )
        
        logging.getLogger().setLevel(log_level)
        
        if app_mode == 'production':
            logging.warning("Logging configured for production mode")
        else:
            logging.info(f"Logging configured for {app_mode} mode")
            
    except Exception as e:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        logging.error(f"Failed to configure logging from config: {e}")

def get_local_timezone_offset():
    return int(datetime.now().astimezone().utcoffset().total_seconds() // 3600)

async def create_daily_directory(record_dir):
    today = datetime.now().strftime('%Y-%m-%d')
    daily_dir = Path(record_dir) / today
    await asyncio.to_thread(daily_dir.mkdir, parents=True, exist_ok=True)
    return daily_dir

def find_video_file(filename):
    config = get_config()
    for camera_config in config.get('cameras', {}).values():
        record_dir = camera_config.get('record_dir')
        if not record_dir:
            continue
        for date_dir in Path(record_dir).glob('????-??-??'):
            file_path = date_dir / filename
            if file_path.exists():
                return file_path
    return None

async def shutdown_handler():
    logging.info("Initiating graceful shutdown...")
    stop_event.set()
    
    # Cancel all background tasks
    if background_tasks:
        logging.info(f"Cancelling {len(background_tasks)} background tasks...")
        for task in list(background_tasks):
            if not task.done():
                task.cancel()
        
        # Wait for all tasks to complete or be cancelled
        try:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        except Exception as e:
            logging.error(f"Error during task cancellation: {e}")
    
    # Disconnect MQTT if connected
    if mqtt_client:
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
            logging.info("MQTT client disconnected")
        except Exception as e:
            logging.error(f"Error disconnecting MQTT: {e}")
    
    shutdown_complete.set()
    logging.info("Graceful shutdown complete")

def signal_handler(signum, frame):
    logging.info(f"Received signal {signum}")
    stop_event.set()
    # Force uvicorn shutdown
    import sys
    sys.exit(0)

async def run_subprocess(cmd, name):
    process = None
    try:
        # Create subprocess with process group for better cleanup
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid if hasattr(os, 'setsid') else None
        )
        
        logging.info(f"Started subprocess for {name} (PID: {process.pid})")
        
        # Wait for process to complete or be cancelled
        returncode = await process.wait()
        
        if returncode != 0:
            stderr = await process.stderr.read()
            logging.error(f"{name} subprocess failed with code {returncode}: {stderr.decode()}")
        
        return returncode == 0
        
    except asyncio.CancelledError:
        logging.info(f"Cancelling subprocess for {name}")
        if process:
            await terminate_process(process, name)
        raise
    except Exception as e:
        logging.error(f"Error in subprocess for {name}: {e}")
        if process:
            await terminate_process(process, name)
        return False

async def terminate_process(process, name, timeout=10):
    if not process or process.returncode is not None:
        return
    
    try:
        # First try SIGTERM
        process.terminate()
        logging.info(f"Sent SIGTERM to {name} (PID: {process.pid})")
        
        # Wait for graceful shutdown
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            logging.info(f"{name} terminated gracefully")
        except asyncio.TimeoutError:
            logging.warning(f"{name} did not terminate gracefully, sending SIGKILL")
            # Escalate to SIGKILL
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
            logging.info(f"{name} killed forcefully")
        
        # Explicitly close subprocess pipes to prevent cleanup warnings
        try:
            if process.stdout and not process.stdout.is_closing():
                process.stdout.close()
        except Exception:
            pass
        try:
            if process.stderr and not process.stderr.is_closing():
                process.stderr.close()
        except Exception:
            pass
        try:
            if process.stdin and not process.stdin.is_closing():
                process.stdin.close()
        except Exception:
            pass
        
    except Exception as e:
        logging.error(f"Error terminating {name}: {e}")

async def record_camera(name, go2rtc_url, record_dir, segment_time):
    while not stop_event.is_set():
        try:
            daily_dir = await create_daily_directory(record_dir)
            
            cmd = [
                'ffmpeg',
                '-rtsp_transport', 'tcp',
                '-use_wallclock_as_timestamps', '1',
                '-i', go2rtc_url,
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-movflags', 'faststart',
                '-metadata', 'title=Cakrawala',
                '-f', 'segment',
                '-segment_time', str(segment_time),
                '-reset_timestamps', '1',
                '-strftime', '1',
                str(daily_dir / f"{name}_%Y-%m-%d_%H-%M-%S.mp4"),
                '-y'
            ]
            
            # Run FFmpeg with process management
            success = await run_subprocess(cmd, f"camera-{name}")
            
            if not success and not stop_event.is_set():
                logging.warning(f"Camera {name}: Recording failed, restarting in 5s...")
                await asyncio.sleep(5)
            
        except asyncio.CancelledError:
            logging.info(f"Camera {name}: Recording cancelled")
            break
        except Exception as e:
            logging.error(f"Camera {name}: Unexpected error: {e}")
            if not stop_event.is_set():
                await asyncio.sleep(5)
    
    logging.info(f"Camera {name}: Recording stopped")

async def get_files_to_delete(record_path, camera_name, cutoff_time):
    """Get list of files to delete and total size (async I/O)"""
    files_to_delete = []
    total_size = 0
    
    if not record_path.exists():
        return files_to_delete, total_size
    
    # Use asyncio for file system operations to avoid blocking
    for file_path in record_path.rglob('*'):
        if stop_event.is_set():
            break
            
        if not file_path.is_file():
            continue
            
        # Filter camera files - only MP4
        if not (file_path.name.startswith(f"{camera_name}_") and file_path.suffix == '.mp4'):
            continue
            
        try:
            file_stat = file_path.stat()
            file_mtime = datetime.fromtimestamp(file_stat.st_mtime)
            
            if file_mtime < cutoff_time:
                files_to_delete.append(file_path)
                total_size += file_stat.st_size
                
        except (OSError, ValueError):
            continue
        
        # Yield control periodically to avoid blocking
        if len(files_to_delete) % 100 == 0:
            await asyncio.sleep(0)
    
    return files_to_delete, total_size

async def delete_files_safely(files_to_delete):
    deleted_count = 0
    deleted_size = 0
    
    for i, file_path in enumerate(files_to_delete):
        if stop_event.is_set():
            break
            
        try:
            file_size = file_path.stat().st_size
            file_path.unlink()
            deleted_count += 1
            deleted_size += file_size
        except (FileNotFoundError, PermissionError, OSError):
            continue
        
        # Yield control periodically to avoid blocking
        if i % 50 == 0:
            await asyncio.sleep(0)
    
    return deleted_count, deleted_size

async def cleanup_empty_directories(record_path: Path):
    if not record_path.exists():
        return
        
    for item in record_path.iterdir():
        if stop_event.is_set():
            break
            
        if not item.is_dir():
            continue
            
        try:
            datetime.strptime(item.name, '%Y-%m-%d')
            if not any(item.iterdir()):
                item.rmdir()
                logging.info(f"Removed empty directory: {item}")
        except (ValueError, OSError):
            continue
        
        await asyncio.sleep(0)  # Yield control

async def cleanup_camera_recordings(name, record_dir, retain_for):
    try:
        record_path = Path(record_dir)
        if not record_path.exists():
            return False
        
        # Calculate cutoff date (retain_for is in seconds)
        retention_delta = timedelta(seconds=int(retain_for))
        cutoff_time = datetime.now() - retention_delta
        
        # Get files to delete (async)
        files_to_delete, total_size_to_delete = await get_files_to_delete(record_path, name, cutoff_time)
        
        if not files_to_delete:
            return True
        
        # Delete files safely (async)
        deleted_count, deleted_size = await delete_files_safely(files_to_delete)
        
        # Clean up empty directories (async)
        await cleanup_empty_directories(record_path)
        
        # Report results
        if deleted_count > 0:
            deleted_mb = deleted_size / (1024 * 1024)
            logging.info(f"Camera {name}: Deleted {deleted_count} files, freed {deleted_mb:.1f} MB")
        
        return True
        
    except Exception as e:
        logging.error(f"Camera {name}: Cleanup error: {e}")
        return False

def should_run_cleanup(name, config, last_cleanup_times):
    try:
        retain_interval = int(config.get('retain_interval', 86400))  # Default 24 hours in seconds
        
        last_cleanup = last_cleanup_times.get(name, 0)
        current_time = asyncio.get_event_loop().time()
        
        return (current_time - last_cleanup) >= retain_interval
        
    except Exception as e:
        logging.error(f"Error checking cleanup interval for {name}: {e}")
        return False

async def check_process_running(process_name):
    try:
        process = await asyncio.create_subprocess_exec(
            'pgrep', '-f', process_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return process.returncode == 0
    except Exception:
        return False

async def go2rtc_worker():
    while not stop_event.is_set():
        try:
            # Check if go2rtc is already running (async way)
            if await check_process_running('go2rtc'):
                logging.debug("go2rtc already running")
                await asyncio.sleep(30)  # Check every 30 seconds
                continue
            
            cmd = ['./bin/go2rtc', '-config', './conf/go2rtc.yaml']
            logging.info("Starting go2rtc process")
            
            success = await run_subprocess(cmd, "go2rtc")
            
            if not success and not stop_event.is_set():
                logging.warning("go2rtc failed, restarting in 10s...")
                await asyncio.sleep(10)
            
        except asyncio.CancelledError:
            logging.info("go2rtc worker cancelled")
            break
        except Exception as e:
            logging.error(f"go2rtc worker error: {e}")
            if not stop_event.is_set():
                await asyncio.sleep(10)
    
    logging.info("go2rtc worker stopped")

async def cleanup_worker():
    while not stop_event.is_set():
        try:
            config = get_config()
            cameras = config.get('cameras', {})
            current_time = asyncio.get_event_loop().time()
            
            for name, camera_config in cameras.items():
                if stop_event.is_set():
                    break
                    
                record_dir = camera_config.get('record_dir')
                retain_for = camera_config.get('retain_for', 2592000)  # Default 30 days in seconds
                
                if not record_dir:
                    continue
                
                # Check if this camera needs cleanup
                if should_run_cleanup(name, camera_config, last_cleanup_times):
                    success = await cleanup_camera_recordings(name, record_dir, retain_for)
                    if success:
                        last_cleanup_times[name] = current_time
                        
        except Exception as e:
            logging.error(f"Error in cleanup worker: {e}")
        
        # Sleep 30s between cleanup checks
        for _ in range(30):
            if stop_event.is_set():
                break
            await asyncio.sleep(1)
    
    logging.info("Cleanup worker stopped")

def connect_mqtt(config):
    global mqtt_client
    
    network_config = config.get('network', {})
    broker = network_config.get('mqtt_broker')
    port = network_config.get('mqtt_port', 1883)
    username = network_config.get('mqtt_user')
    password = network_config.get('mqtt_pswd')
    
    if not broker:
        return None
    
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        
        if username and password:
            client.username_pw_set(username, password)
        
        client.connect(broker, port, 60)
        client.loop_start()
        mqtt_client = client
        logging.info(f"Connected to MQTT broker: {broker}:{port}")
        return client
    except Exception as e:
        logging.error(f"Failed to connect to MQTT broker: {e}")
        if 'client' in locals():
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        return None

def get_navigation_status(status_code):
    status_map = {
        0: "Under way using engine", 1: "At anchor", 2: "Not under command",
        3: "Restricted manoeuvrability", 4: "Constrained by her draught", 5: "Moored",
        6: "Aground", 7: "Engaged in fishing", 8: "Under way sailing",
        14: "AIS-SART", 15: "Undefined"
    }
    return status_map.get(status_code, f"Unknown ({status_code})")

def get_ship_type_name(ship_type):
    if ship_type is None:
        return "Unknown"
    
    type_map = {
        30: "Fishing", 31: "Towing", 36: "Sailing", 37: "Pleasure Craft",
        50: "Pilot Vessel", 52: "Tug", 60: "Passenger", 70: "Cargo", 80: "Tanker"
    }
    return type_map.get(ship_type, f"Type {ship_type}")

def decode_ais_message(raw_message, timezone_offset):
    try:
        message = raw_message.strip()
        if not message.startswith('!AIVD'):
            return None
            
        decoded = decode(message)
        
        if not hasattr(decoded, 'mmsi'):
            return None
            
        vessel_data = {
            'mmsi': decoded.mmsi,
            'msg_type': decoded.msg_type,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'timezone_offset': timezone_offset,
            'raw_message': message
        }
        
        # Position reports (Types 1, 2, 3, 18, 19)
        if decoded.msg_type in [1, 2, 3, 18, 19]:
            if hasattr(decoded, 'lat') and hasattr(decoded, 'lon'):
                if decoded.lat != 91.0 and decoded.lon != 181.0 and decoded.lat != 0.0 and decoded.lon != 0.0:
                    vessel_data.update({
                        'latitude': decoded.lat,
                        'longitude': decoded.lon,
                    })
                    
                    if hasattr(decoded, 'speed') and decoded.speed != 1023:
                        vessel_data['speed'] = decoded.speed / 10.0
                    
                    if hasattr(decoded, 'course') and decoded.course != 3600:
                        vessel_data['course'] = decoded.course / 10.0
                        
                    if hasattr(decoded, 'heading') and decoded.heading != 511:
                        vessel_data['heading'] = decoded.heading
                        
                    if hasattr(decoded, 'status'):
                        vessel_data['nav_status'] = decoded.status
                        vessel_data['nav_status_text'] = get_navigation_status(decoded.status)
        
        # Static data (Type 5)
        elif decoded.msg_type == 5:
            if hasattr(decoded, 'shipname'):
                vessel_data['shipname'] = decoded.shipname.strip('@')
            if hasattr(decoded, 'ship_type'):
                vessel_data['ship_type'] = decoded.ship_type
                vessel_data['ship_type_text'] = get_ship_type_name(decoded.ship_type)
            if hasattr(decoded, 'callsign'):
                vessel_data['callsign'] = decoded.callsign.strip('@')
            if hasattr(decoded, 'destination'):
                vessel_data['destination'] = decoded.destination.strip('@')
                
            if hasattr(decoded, 'to_bow'):
                vessel_data['length'] = decoded.to_bow + getattr(decoded, 'to_stern', 0)
                vessel_data['beam'] = getattr(decoded, 'to_port', 0) + getattr(decoded, 'to_starboard', 0)
                
            if hasattr(decoded, 'draught') and decoded.draught != 0:
                vessel_data['draught'] = decoded.draught / 10.0
        
        return vessel_data
                
    except Exception:
        return None

async def init_database():
    """Initialize the SQLite database (async)"""
    try:
        # Use asyncio to avoid blocking
        loop = asyncio.get_event_loop()
        
        def _create_tables():
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS vessels (
                    mmsi INTEGER PRIMARY KEY,
                    shipname TEXT,
                    ship_type INTEGER,
                    ship_type_text TEXT,
                    callsign TEXT,
                    destination TEXT,
                    length INTEGER,
                    beam INTEGER,
                    draught REAL,
                    first_seen TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS vessel_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mmsi INTEGER,
                    latitude REAL,
                    longitude REAL,
                    speed REAL,
                    course REAL,
                    heading INTEGER,
                    nav_status INTEGER,
                    nav_status_text TEXT,
                    timestamp TIMESTAMP,
                    timezone_offset INTEGER,
                    FOREIGN KEY (mmsi) REFERENCES vessels(mmsi)
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mmsi_timestamp ON vessel_movements(mmsi, timestamp)')
            
            conn.commit()
            conn.close()
        
        await loop.run_in_executor(None, _create_tables)
        logging.info("Database initialized")
        
    except Exception as e:
        logging.error(f"Failed to initialize database: {e}")

def get_db_connection():
    """Get database connection with proper configuration"""
    conn = sqlite3.connect('data.db')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn

async def save_vessel_to_db(mmsi, vessel_data):
    """Save vessel data to database (async)"""
    try:
        loop = asyncio.get_event_loop()
        
        def _save_vessel():
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Update vessel static data
            cursor.execute('''
                INSERT OR REPLACE INTO vessels (
                    mmsi, shipname, ship_type, ship_type_text, callsign,
                    destination, length, beam, draught, first_seen, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 
                         COALESCE((SELECT first_seen FROM vessels WHERE mmsi = ?), ?), 
                         CURRENT_TIMESTAMP)
            ''', (
                mmsi,
                vessel_data.get('shipname'),
                vessel_data.get('ship_type'),
                vessel_data.get('ship_type_text'),
                vessel_data.get('callsign'),
                vessel_data.get('destination'),
                vessel_data.get('length'),
                vessel_data.get('beam'),
                vessel_data.get('draught'),
                mmsi,
                vessel_data.get('first_seen')
            ))
            
            # Insert movement data if position available
            if vessel_data.get('latitude') is not None and vessel_data.get('longitude') is not None:
                cursor.execute('''
                    INSERT INTO vessel_movements (
                        mmsi, latitude, longitude, speed, course, heading,
                        nav_status, nav_status_text, timestamp, timezone_offset
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    mmsi,
                    vessel_data.get('latitude'),
                    vessel_data.get('longitude'),
                    vessel_data.get('speed'),
                    vessel_data.get('course'),
                    vessel_data.get('heading'),
                    vessel_data.get('nav_status'),
                    vessel_data.get('nav_status_text'),
                    vessel_data.get('timestamp'),
                    vessel_data.get('timezone_offset')
                ))
            
            conn.commit()
            conn.close()
        
        await loop.run_in_executor(None, _save_vessel)
    except Exception as e:
        logging.error(f"Failed to save vessel {mmsi}: {e}")

async def broadcast_vessel_update(vessel_data):
    """Broadcast vessel update to WebSocket connections"""
    global websocket_connections
    
    if not websocket_connections:
        return
        
    message = json.dumps({
        'type': 'vessel_update',
        'data': vessel_data
    })
    
    disconnected = set()
    for websocket in list(websocket_connections):
        try:
            await websocket.send_text(message)
        except Exception as e:
            logging.debug(f"WebSocket send failed: {e}")
            disconnected.add(websocket)
    
    for ws in disconnected:
        websocket_connections.discard(ws)

async def update_vessel_database(vessel_data):
    """Update vessel database and broadcast updates"""
    global ais_vessels, websocket_connections
    
    if not vessel_data or 'mmsi' not in vessel_data:
        return
    
    mmsi = vessel_data['mmsi']
    
    if mmsi not in ais_vessels:
        ais_vessels[mmsi] = {
            'mmsi': mmsi,
            'first_seen': vessel_data['timestamp']
        }
    
    ais_vessels[mmsi].update(vessel_data)
    ais_vessels[mmsi]['last_seen'] = vessel_data['timestamp']
    
    await save_vessel_to_db(mmsi, ais_vessels[mmsi])
    
    # Broadcast to WebSocket connections
    if websocket_connections:
        await broadcast_vessel_update(ais_vessels[mmsi])

async def ais_tcp_worker(ais_config, lpu_config, network_config):
    """AIS TCP connection worker (async)"""
    ip = ais_config.get('ip')
    port = ais_config.get('port')
    timezone_offset = get_local_timezone_offset()
    
    # Connect to MQTT (still sync for now)
    local_mqtt_client = connect_mqtt({'network': network_config})
    
    while not stop_event.is_set():
        try:
            logging.info(f"Connecting to AIS server: {ip}:{port}")
            
            # Use async approach for TCP connection
            reader, writer = await asyncio.open_connection(ip, port)
            
            try:
                while not stop_event.is_set():
                    # Read data with timeout
                    try:
                        data = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                        if not data:
                            break
                        
                        # Process each line
                        for line in data.decode('utf-8', errors='ignore').splitlines():
                            if stop_event.is_set():
                                break
                                
                            if '!AIVD' in line:
                                vessel_data = decode_ais_message(line, timezone_offset)
                                if vessel_data:
                                    await update_vessel_database(vessel_data)
                        
                    except asyncio.TimeoutError:
                        continue  # No data received, check stop_event and continue
                        
            finally:
                writer.close()
                await writer.wait_closed()
                
        except Exception as e:
            logging.error(f"AIS connection error: {e}")
            if not stop_event.is_set():
                await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logging.info("LPU starting...")
    
    config = get_config()
    cameras = config.get('cameras', {})
    ais_config = config.get('ais', {})
    lpu_config = config.get('lpu', {})
    network_config = config.get('network', {})
    
    # Prepare directories
    await asyncio.to_thread(Path('views').mkdir, exist_ok=True)
    await asyncio.to_thread(Path('views/static').mkdir, exist_ok=True)
    
    # Start background tasks
    try:
        # Start go2rtc worker FIRST (must run before everything else)
        go2rtc_task = asyncio.create_task(go2rtc_worker())
        background_tasks.add(go2rtc_task)
        go2rtc_task.add_done_callback(background_tasks.discard)
        logging.info("go2rtc worker started")
        
        # Wait a bit for go2rtc to start up before starting cameras
        await asyncio.sleep(3)
        
        # Start recording tasks
        for name, camera_config in cameras.items():
            segment_duration = camera_config.get('segment', 30)
            go2rtc_url = f"rtsp://localhost:9554/{name}"
            
            logging.info(f"Starting recording: {name}")
            
            task = asyncio.create_task(
                record_camera(name, go2rtc_url, camera_config['record_dir'], segment_duration)
            )
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
        
        # Start cleanup worker
        cleanup_task = asyncio.create_task(cleanup_worker())
        background_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(background_tasks.discard)
        
        # Initialize AIS if configured
        if ais_config and ais_config.get('ip') and ais_config.get('port'):
            await init_database()
            ais_task = asyncio.create_task(
                ais_tcp_worker(ais_config, lpu_config, network_config)
            )
            background_tasks.add(ais_task)
            ais_task.add_done_callback(background_tasks.discard)
            logging.info("AIS tracking enabled")
        else:
            logging.info("AIS tracking disabled")
        
        logging.info(f"Started: {len(cameras)} cameras + cleanup + web + AIS")
        
        yield
        
    finally:
        # Shutdown
        await shutdown_handler()
        await shutdown_complete.wait()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory="views")
app.mount("/static", StaticFiles(directory="views/static"), name="static")

def get_recordings_by_date(date_str):
    """Get recordings for a specific date, grouped by camera and hour"""
    config = get_config()
    cameras = config.get('cameras', {})
    recordings = {}
    
    for camera_name, camera_config in cameras.items():
        record_dir = camera_config.get('record_dir')
        if not record_dir:
            continue
            
        daily_path = Path(record_dir) / date_str
        if not daily_path.exists():
            recordings[camera_name] = {}
            continue
            
        camera_recordings = defaultdict(list)
        pattern = f"{camera_name}_*.mp4"
        files = list(daily_path.glob(pattern))
        
        for file_path in sorted(files):
            try:
                parts = file_path.stem.split('_')
                if len(parts) >= 3:
                    time_part = parts[2]
                    hour = time_part.split('-')[0]
                    
                    file_info = {
                        'path': str(file_path),
                        'filename': file_path.name,
                        'timestamp': f"{parts[1]}_{parts[2]}",
                        'size': file_path.stat().st_size,
                        'mtime': file_path.stat().st_mtime
                    }
                    camera_recordings[hour].append(file_info)
            except (IndexError, ValueError):
                continue
                
        recordings[camera_name] = dict(camera_recordings)
    
    return recordings

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main NVR page"""
    config = get_config()
    cameras = list(config.get('cameras', {}).keys())
    
    today = datetime.now().strftime('%Y-%m-%d')
    recordings = get_recordings_by_date(today)
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "cameras": cameras,
            "recordings": recordings,
            "selected_date": today
        }
    )

@app.get("/api/recordings/available")
async def api_available_dates():
    """Get all dates that have recordings available"""
    config = get_config()
    cameras = config.get('cameras', {})
    available_dates = set()
    
    for camera_config in cameras.values():
        record_dir = camera_config.get('record_dir')
        if not record_dir:
            continue
        
        record_path = Path(record_dir)
        if not record_path.exists():
            continue
        
        # Find date directories (YYYY-MM-DD format) with recordings
        for date_dir in record_path.glob('????-??-??'):
            if date_dir.is_dir():
                # Check if directory has any MP4 files
                if any(date_dir.glob('*.mp4')):
                    available_dates.add(date_dir.name)
    
    return sorted(list(available_dates))

@app.get("/api/recordings/{date}")
async def api_recordings(date):
    """API endpoint to get recordings for a specific date"""
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")
    
    recordings = get_recordings_by_date(date)
    return recordings

@app.get("/api/video/{filename}")
async def api_video(filename):
    """Serve MP4 video file"""
    if not filename.endswith('.mp4') or not '_' in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    video_file = find_video_file(filename)
    if not video_file:
        raise HTTPException(status_code=404, detail="Video not found")
    
    return FileResponse(video_file, media_type='video/mp4')

@app.get("/api/stream/{src}")
async def stream_proxy(src):
    """Stream proxy to go2rtc"""
    if not src.replace('_', '').isalnum():
        raise HTTPException(status_code=400, detail="Invalid src parameter")
    
    # Direct proxy without aiohttp dependency
    import urllib.request
    
    try:
        go2rtc_url = f"http://localhost:1984/api/stream.mp4?src={src}"
        
        async def stream_generator():
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as client:
                    async with client.stream('GET', go2rtc_url) as response:
                        if response.status_code != 200:
                            return
                        async for chunk in response.aiter_bytes(4096):
                            yield chunk
            except Exception as e:
                logging.error(f"Stream proxy error for {src}: {e}")        
        
        return StreamingResponse(stream_generator(), media_type="video/mp4")
    except Exception:
        raise HTTPException(status_code=503, detail="Stream unavailable")

@app.get("/ais", response_class=HTMLResponse)
async def ais_page(request: Request):
    """AIS tracking page"""
    config = get_config()
    return templates.TemplateResponse("ais.html", {"request": request, "config": config})

@app.get("/api/ais/vessels")
async def api_ais_vessels():
    """Get current AIS vessel data"""
    try:
        if not os.path.exists('data.db'):
            return {}
        
        loop = asyncio.get_event_loop()
        
        def _get_vessels():
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT 
                    v.mmsi, v.shipname, v.ship_type_text, v.callsign, v.destination,
                    v.length, v.beam, v.draught, v.first_seen, v.last_updated,
                    m.latitude, m.longitude, m.speed, m.course, m.heading,
                    m.nav_status_text, m.timestamp, m.timezone_offset
                FROM vessels v
                LEFT JOIN (
                    SELECT mmsi, latitude, longitude, speed, course, heading,
                           nav_status_text, timestamp, timezone_offset,
                           ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY timestamp DESC) as rn
                    FROM vessel_movements
                    WHERE datetime(timestamp) > datetime('now', '-30 minutes')
                ) m ON v.mmsi = m.mmsi AND m.rn = 1
                WHERE m.mmsi IS NOT NULL
            ''')
            
            vessels = {}
            for row in cursor.fetchall():
                vessels[row[0]] = {
                    'mmsi': row[0], 'shipname': row[1], 'ship_type_text': row[2],
                    'callsign': row[3], 'destination': row[4], 'length': row[5],
                    'beam': row[6], 'draught': row[7], 'first_seen': row[8],
                    'last_updated': row[9], 'latitude': row[10], 'longitude': row[11],
                    'speed': row[12], 'course': row[13], 'heading': row[14],
                    'nav_status_text': row[15], 'timestamp': row[16], 'timezone_offset': row[17]
                }
            
            conn.close()
            return vessels
        
        vessels = await loop.run_in_executor(None, _get_vessels)
        return vessels
    except Exception as e:
        logging.error(f"Failed to get AIS vessels: {e}")
        return {}

@app.websocket("/ws/ais")
async def websocket_ais(websocket: WebSocket):
    """WebSocket endpoint for real-time AIS updates"""
    client_ip = websocket.client.host if websocket.client else "unknown"
    
    await websocket.accept()
    logging.info(f"AIS WebSocket connected from {client_ip}")
    
    try:
        websocket_connections.add(websocket)
        
        # Send initial data
        vessels = await api_ais_vessels()
        await websocket.send_text(json.dumps({
            'type': 'initial_data',
            'data': vessels
        }))
        logging.debug(f"Sent initial AIS data to {client_ip} ({len(vessels)} vessels)")
        
        # Keep connection alive
        while True:
            try:
                message = await websocket.receive_text()
                data = json.loads(message)
                
                if data.get('type') == 'ping':
                    await websocket.send_text(json.dumps({'type': 'pong'}))
                    
            except WebSocketDisconnect:
                logging.info(f"AIS WebSocket disconnected from {client_ip}")
                break
                
    except Exception as e:
        logging.error(f"AIS WebSocket error from {client_ip}: {e}")
    finally:
        websocket_connections.discard(websocket)
        logging.debug(f"AIS WebSocket cleanup completed for {client_ip}")

def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    configure_logging()
    
    config = get_config()
    lite_config = config.get('lite', {})
    lpu_config = config.get('lpu', {})
    
    app_mode = lpu_config.get('app_mode', 'development').lower()
    uvicorn_log_level = "warning" if app_mode == 'production' else "info"
    
    port = lite_config.get('port', 9191)
    logging.info(f"Starting web interface on port {port}")
    
    def signal_shutdown(signum, frame):
        logging.info(f"Received signal {signum}")
        stop_event.set()
        # Force uvicorn to shutdown
        import sys
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, signal_shutdown)
    signal.signal(signal.SIGINT, signal_shutdown)
    
    import uvicorn
    
    try:
        uvicorn.run(app, host='0.0.0.0', port=port, log_level=uvicorn_log_level)
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received")
        stop_event.set()
    except Exception as e:
        logging.error(f"Server error: {e}")
        stop_event.set()
    finally:
        logging.info("Server shutdown complete")

if __name__ == '__main__':
    main()