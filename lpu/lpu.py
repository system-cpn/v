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

# AIS message fragment management
ais_message_fragments = {}  # Store incomplete multi-part messages
fragment_cleanup_time = 0   # Last cleanup timestamp

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
    """Enhanced shutdown handler with proper process cleanup sequence"""
    logging.info("Initiating graceful shutdown...")
    stop_event.set()
    
    try:
        # Phase 1: Immediately terminate all active subprocesses
        await terminate_all_subprocesses()
        
        # Phase 2: Cancel background tasks but don't wait yet
        cancelled_tasks = []
        if background_tasks:
            logging.info(f"Cancelling {len(background_tasks)} background tasks...")
            for task in list(background_tasks):
                if not task.done():
                    task.cancel()
                    cancelled_tasks.append(task)
        
        # Phase 3: Wait for tasks with timeout
        if cancelled_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*cancelled_tasks, return_exceptions=True),
                    timeout=5.0  # 5 second timeout for task cancellation
                )
                logging.info("All background tasks cancelled successfully")
            except asyncio.TimeoutError:
                logging.warning("Some background tasks did not cancel within timeout")
            except Exception as e:
                logging.error(f"Error during task cancellation: {e}")
        
        # Phase 4: Disconnect MQTT
        if mqtt_client:
            try:
                mqtt_client.loop_stop()
                mqtt_client.disconnect()
                logging.info("MQTT client disconnected")
            except Exception as e:
                logging.error(f"Error disconnecting MQTT: {e}")
        
        logging.info("Graceful shutdown complete")
        
    except Exception as e:
        logging.error(f"Error during shutdown: {e}")
    finally:
        shutdown_complete.set()

async def terminate_all_subprocesses():
    """Terminate all active subprocesses immediately"""
    if hasattr(run_subprocess, 'active_processes'):
        processes = list(run_subprocess.active_processes.items())
        if processes:
            logging.info(f"Terminating {len(processes)} active subprocess(es)...")
            
            # Create termination tasks for all processes
            termination_tasks = []
            for name, process in processes:
                if process and process.returncode is None:
                    task = asyncio.create_task(terminate_process_graceful(process, name, timeout=3))
                    termination_tasks.append(task)
            
            # Wait for all terminations with overall timeout
            if termination_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*termination_tasks, return_exceptions=True),
                        timeout=8.0  # Total 8 seconds for all subprocess termination
                    )
                    logging.info("All subprocesses terminated successfully")
                except asyncio.TimeoutError:
                    logging.warning("Some subprocesses did not terminate within timeout")
                except Exception as e:
                    logging.error(f"Error terminating subprocesses: {e}")
            
            # Clear the active processes dict
            run_subprocess.active_processes.clear()
    else:
        logging.info("No active subprocesses to terminate")

def signal_handler(signum, frame):
    """Handle shutdown signals more gracefully"""
    logging.info(f"Received signal {signum}")
    stop_event.set()
    
    # Don't force exit immediately - let the lifespan handler manage shutdown
    # This prevents the "Event loop is closed" errors
    logging.info("Signal received, initiating graceful shutdown sequence...")

async def run_subprocess(cmd, name, needs_stdin=False):
    process = None
    try:
        # Create subprocess with process group for better cleanup
        stdin_pipe = asyncio.subprocess.PIPE if needs_stdin else None
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=stdin_pipe,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid if hasattr(os, 'setsid') else None
        )
        
        logging.info(f"Started subprocess for {name} (PID: {process.pid})")
        
        # Store process reference for cleanup
        if not hasattr(run_subprocess, 'active_processes'):
            run_subprocess.active_processes = {}
        run_subprocess.active_processes[name] = process
        
        # Wait for process to complete or be cancelled
        returncode = await process.wait()
        
        # Remove from active processes
        if hasattr(run_subprocess, 'active_processes') and name in run_subprocess.active_processes:
            del run_subprocess.active_processes[name]
        
        if returncode != 0 and not stop_event.is_set():
            stderr = await process.stderr.read()
            logging.error(f"{name} subprocess failed with code {returncode}: {stderr.decode()}")
        
        return returncode == 0
        
    except asyncio.CancelledError:
        logging.info(f"Cancelling subprocess for {name}")
        if process:
            await terminate_process_graceful(process, name)
        # Remove from active processes
        if hasattr(run_subprocess, 'active_processes') and name in run_subprocess.active_processes:
            del run_subprocess.active_processes[name]
        raise
    except Exception as e:
        logging.error(f"Error in subprocess for {name}: {e}")
        if process:
            await terminate_process_graceful(process, name)
        # Remove from active processes
        if hasattr(run_subprocess, 'active_processes') and name in run_subprocess.active_processes:
            del run_subprocess.active_processes[name]
        return False

async def terminate_process_graceful(process, name, timeout=5):
    """Enhanced process termination with FFmpeg-specific handling"""
    if not process or process.returncode is not None:
        return
    
    try:
        # Special handling for FFmpeg processes
        if 'ffmpeg' in name.lower() or 'camera' in name.lower():
            await terminate_ffmpeg_process(process, name, timeout)
        else:
            await terminate_generic_process(process, name, timeout)
            
    except Exception as e:
        logging.error(f"Error terminating {name}: {e}")
        # Force cleanup even if termination fails
        await cleanup_process_pipes(process)

async def terminate_ffmpeg_process(process, name, timeout=5):
    """FFmpeg-specific graceful termination"""
    try:
        # Step 1: Try to send 'q' command to FFmpeg stdin for graceful stop
        if process.stdin and not process.stdin.is_closing():
            try:
                process.stdin.write(b'q\n')
                await process.stdin.drain()
                logging.info(f"Sent graceful quit command to {name}")
                
                # Give FFmpeg 2 seconds to respond to 'q' command
                await asyncio.wait_for(process.wait(), timeout=2)
                logging.info(f"{name} terminated gracefully via quit command")
                await cleanup_process_pipes(process)
                return
            except (asyncio.TimeoutError, BrokenPipeError, OSError):
                # FFmpeg didn't respond to 'q', continue to SIGTERM
                pass
        
        # Step 2: Send SIGTERM
        if process.returncode is None:
            process.terminate()
            logging.info(f"Sent SIGTERM to {name} (PID: {process.pid})")
            
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
                logging.info(f"{name} terminated gracefully")
            except asyncio.TimeoutError:
                # Step 3: Force kill if SIGTERM doesn't work
                logging.warning(f"{name} did not terminate gracefully, sending SIGKILL")
                try:
                    # Try process group kill first
                    if hasattr(os, 'killpg'):
                        try:
                            os.killpg(os.getpgid(process.pid), 9)
                            logging.info(f"Sent SIGKILL to process group for {name}")
                        except (ProcessLookupError, OSError):
                            # Process group doesn't exist, try individual kill
                            process.kill()
                            logging.info(f"Sent SIGKILL to {name}")
                    else:
                        process.kill()
                        logging.info(f"Sent SIGKILL to {name}")
                    
                    await asyncio.wait_for(process.wait(), timeout=2)
                    logging.info(f"{name} killed forcefully")
                except (ProcessLookupError, OSError) as e:
                    # Process already dead or doesn't exist
                    logging.debug(f"Process {name} already terminated: {e}")
                except asyncio.TimeoutError:
                    # Process is really stuck, give up
                    logging.error(f"Process {name} did not respond to SIGKILL, abandoning")
        
        await cleanup_process_pipes(process)
        
    except Exception as e:
        logging.error(f"Error in FFmpeg termination for {name}: {e}")
        await cleanup_process_pipes(process)

async def terminate_generic_process(process, name, timeout=5):
    """Generic process termination"""
    try:
        # Send SIGTERM
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
            await asyncio.wait_for(process.wait(), timeout=2)
            logging.info(f"{name} killed forcefully")
        
        await cleanup_process_pipes(process)
        
    except Exception as e:
        logging.error(f"Error in generic termination for {name}: {e}")
        await cleanup_process_pipes(process)

async def cleanup_process_pipes(process):
    """Safely cleanup process pipes with exception handling"""
    try:
        # Close pipes safely with protection against event loop closure
        for pipe_name, pipe in [('stdin', process.stdin), ('stdout', process.stdout), ('stderr', process.stderr)]:
            if pipe and not pipe.is_closing():
                try:
                    pipe.close()
                    # Don't wait for pipe to close if event loop is closing
                    if not asyncio.get_event_loop().is_closed():
                        try:
                            await asyncio.wait_for(pipe.wait_closed(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass
                except RuntimeError as e:
                    if "Event loop is closed" in str(e):
                        # Ignore event loop closure errors during shutdown
                        pass
                    else:
                        raise
                except Exception:
                    # Ignore other pipe cleanup errors
                    pass
    except Exception:
        # Ignore all pipe cleanup errors to prevent blocking shutdown
        pass

# Keep the old function for backward compatibility
async def terminate_process(process, name, timeout=10):
    """Legacy function - redirect to new graceful termination"""
    await terminate_process_graceful(process, name, timeout)

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
            
            # Run FFmpeg with process management (needs stdin for graceful quit)
            success = await run_subprocess(cmd, f"camera-{name}", needs_stdin=True)
            
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
            
            cmd = ['go2rtc', '-config', './conf/go2rtc.yaml']
            logging.info("Starting go2rtc process")
            
            success = await run_subprocess(cmd, "go2rtc", needs_stdin=False)
            
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
        # Vessels not engaged in fishing
        20: "Wing In Ground", 21: "Wing In Ground (Hazardous)", 22: "Wing In Ground (Hazardous)",
        23: "Wing In Ground (Hazardous)", 24: "Wing In Ground (Hazardous)", 25: "Wing In Ground (Hazardous)",
        26: "Wing In Ground (Hazardous)", 27: "Wing In Ground (Hazardous)", 28: "Wing In Ground (Hazardous)",
        29: "Wing In Ground (Hazardous)",
        
        # Fishing vessels
        30: "Fishing",
        
        # Towing vessels  
        31: "Towing", 32: "Towing (>200m)",
        
        # Other vessels
        33: "Dredging", 34: "Diving", 35: "Military", 36: "Sailing", 37: "Pleasure Craft",
        
        # High Speed Craft
        40: "High Speed Craft", 41: "High Speed Craft (Hazardous)", 42: "High Speed Craft (Hazardous)",
        43: "High Speed Craft (Hazardous)", 44: "High Speed Craft (Hazardous)", 45: "High Speed Craft (Hazardous)",
        46: "High Speed Craft (Hazardous)", 47: "High Speed Craft (Hazardous)", 48: "High Speed Craft (Hazardous)",
        49: "High Speed Craft (Hazardous)",
        
        # Special vessels
        50: "Pilot Vessel", 51: "Search and Rescue", 52: "Tug", 53: "Port Tender", 54: "Anti-pollution",
        55: "Law Enforcement", 56: "Spare", 57: "Spare", 58: "Medical Transport", 59: "Noncombatant",
        
        # Passenger vessels
        60: "Passenger", 61: "Passenger (Hazardous)", 62: "Passenger (Hazardous)",
        63: "Passenger (Hazardous)", 64: "Passenger (Hazardous)", 65: "Passenger (Hazardous)",
        66: "Passenger (Hazardous)", 67: "Passenger (Hazardous)", 68: "Passenger (Hazardous)",
        69: "Passenger (Hazardous)",
        
        # Cargo vessels
        70: "Cargo", 71: "Cargo (Hazardous)", 72: "Cargo (Hazardous)", 73: "Cargo (Hazardous)",
        74: "Cargo (Hazardous)", 75: "Cargo (Hazardous)", 76: "Cargo (Hazardous)",
        77: "Cargo (Hazardous)", 78: "Cargo (Hazardous)", 79: "Cargo (Hazardous)",
        
        # Tankers
        80: "Tanker", 81: "Tanker (Hazardous)", 82: "Tanker (Hazardous)", 83: "Tanker (Hazardous)",
        84: "Tanker (Hazardous)", 85: "Tanker (Hazardous)", 86: "Tanker (Hazardous)",
        87: "Tanker (Hazardous)", 88: "Tanker (Hazardous)", 89: "Tanker (Hazardous)",
        
        # Other types
        90: "Other", 91: "Other", 92: "Other", 93: "Other", 94: "Other",
        95: "Other", 96: "Other", 97: "Other", 98: "Other", 99: "Other"
    }
    return type_map.get(ship_type, f"Type {ship_type}")

def calculate_range_bearing(lat1, lon1, lat2, lon2):
    """Calculate range (meters) and bearing (degrees) between two coordinates"""
    import math
    
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    # Calculate range using Haversine formula
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    range_meters = 6371000 * c  # Earth radius in meters
    
    # Calculate bearing
    y = math.sin(dlon) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon)
    bearing_rad = math.atan2(y, x)
    bearing_degrees = (math.degrees(bearing_rad) + 360) % 360
    
    return range_meters, bearing_degrees

class AISMessageFragment:
    """Represents a fragment of a multi-part AIS message"""
    def __init__(self, raw_message, total_fragments, fragment_number, sequence_id, channel):
        self.raw_message = raw_message
        self.total_fragments = total_fragments
        self.fragment_number = fragment_number
        self.sequence_id = sequence_id
        self.channel = channel
        self.timestamp = datetime.now(timezone.utc)
        
    def is_expired(self, timeout_seconds=30):
        """Check if fragment has expired"""
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds() > timeout_seconds

def parse_nmea_sentence(message):
    """Parse NMEA sentence to extract fragment information"""
    try:
        # NMEA format: !AIVDM,total,fragment,sequence_id,channel,payload,checksum
        if not message.startswith('!AIVD'):
            return None
            
        parts = message.split(',')
        if len(parts) < 6:
            return None
            
        total_fragments = int(parts[1])
        fragment_number = int(parts[2])
        sequence_id = parts[3] if parts[3] else None
        channel = parts[4]
        
        return {
            'total_fragments': total_fragments,
            'fragment_number': fragment_number,
            'sequence_id': sequence_id,
            'channel': channel,
            'raw_message': message
        }
    except (ValueError, IndexError):
        return None

def get_fragment_key(sequence_id, channel):
    """Generate unique key for fragment group"""
    return f"{sequence_id}_{channel}" if sequence_id else f"no_seq_{channel}"

def assemble_ais_message(fragments):
    """Assemble complete AIS message from fragments"""
    try:
        # Sort fragments by fragment number
        sorted_fragments = sorted(fragments, key=lambda f: f.fragment_number)
        
        # Verify we have all fragments
        expected_numbers = set(range(1, sorted_fragments[0].total_fragments + 1))
        actual_numbers = set(f.fragment_number for f in sorted_fragments)
        
        if expected_numbers != actual_numbers:
            missing = expected_numbers - actual_numbers
            logging.debug(f"Missing fragments: {sorted(missing)}")
            return None
            
        # Extract payloads and reconstruct message
        first_fragment = sorted_fragments[0].raw_message
        base_parts = first_fragment.split(',')
        
        # Combine payloads from all fragments
        combined_payload = ""
        for fragment in sorted_fragments:
            parts = fragment.raw_message.split(',')
            if len(parts) >= 6:
                payload = parts[5]
                # Remove checksum from last fragment
                if fragment.fragment_number == fragment.total_fragments:
                    if '*' in payload:
                        payload = payload.split('*')[0]
                combined_payload += payload
        
        # Reconstruct complete message
        # Change to single fragment format
        base_parts[1] = "1"  # total fragments
        base_parts[2] = "1"  # fragment number
        base_parts[3] = ""   # sequence id (empty for single)
        base_parts[5] = combined_payload
        
        # Recalculate checksum
        message_without_checksum = ','.join(base_parts)
        checksum = calculate_nmea_checksum(message_without_checksum)
        
        complete_message = f"{message_without_checksum}*{checksum:02X}"
        
        logging.debug(f"Assembled complete message from {len(fragments)} fragments")
        return complete_message
        
    except Exception as e:
        logging.error(f"Error assembling AIS message: {e}")
        return None

def calculate_nmea_checksum(sentence):
    """Calculate NMEA 0183 checksum"""
    # Remove the leading ! or $ and calculate XOR of all characters
    if sentence.startswith(('!', '$')):
        sentence = sentence[1:]
    
    checksum = 0
    for char in sentence:
        checksum ^= ord(char)
    
    return checksum

def cleanup_expired_fragments():
    """Remove expired message fragments to prevent memory buildup"""
    global ais_message_fragments, fragment_cleanup_time
    
    current_time = datetime.now(timezone.utc)
    
    # Only cleanup every 60 seconds
    if (current_time.timestamp() - fragment_cleanup_time) < 60:
        return
        
    fragment_cleanup_time = current_time.timestamp()
    
    expired_keys = []
    for key, fragments in ais_message_fragments.items():
        # Remove fragments older than 30 seconds
        fragments[:] = [f for f in fragments if not f.is_expired(30)]
        
        # Remove empty fragment groups
        if not fragments:
            expired_keys.append(key)
    
    for key in expired_keys:
        del ais_message_fragments[key]
    
    if expired_keys:
        logging.debug(f"Cleaned up {len(expired_keys)} expired fragment groups")
        
    # Log buffer statistics periodically
    if len(ais_message_fragments) > 0:
        total_fragments = sum(len(fragments) for fragments in ais_message_fragments.values())
        logging.debug(f"Fragment buffer: {len(ais_message_fragments)} groups, {total_fragments} total fragments")

def process_ais_message_with_fragments(raw_message, timezone_offset):
    """Process AIS message with fragment handling support"""
    try:
        # Parse NMEA sentence
        nmea_info = parse_nmea_sentence(raw_message)
        if not nmea_info:
            return None
            
        # Single fragment message - process directly
        if nmea_info['total_fragments'] == 1:
            return decode_ais_message(raw_message, timezone_offset)
            
        # Multi-fragment message - handle assembly
        fragment_key = get_fragment_key(nmea_info['sequence_id'], nmea_info['channel'])
        
        # Create fragment object
        fragment = AISMessageFragment(
            nmea_info['raw_message'],
            nmea_info['total_fragments'],
            nmea_info['fragment_number'],
            nmea_info['sequence_id'],
            nmea_info['channel']
        )
        
        # Add to fragment buffer
        if fragment_key not in ais_message_fragments:
            ais_message_fragments[fragment_key] = []
            logging.debug(f"Started new fragment group: seq={nmea_info['sequence_id']}, channel={nmea_info['channel']}, expecting {nmea_info['total_fragments']} fragments")
            
        ais_message_fragments[fragment_key].append(fragment)
        logging.debug(f"Added fragment {nmea_info['fragment_number']}/{nmea_info['total_fragments']} to group {fragment_key}")
        
        # Check if we have all fragments
        fragments = ais_message_fragments[fragment_key]
        if len(fragments) == nmea_info['total_fragments']:
            # Assemble complete message
            complete_message = assemble_ais_message(fragments)
            
            # Remove from buffer
            del ais_message_fragments[fragment_key]
            
            if complete_message:
                logging.info(f"Assembled complete AIS message from {len(fragments)} fragments (seq: {nmea_info['sequence_id']}, channel: {nmea_info['channel']})")
                # Log the first part of the assembled message for verification
                logging.debug(f"Assembled message: {complete_message[:80]}...")
                return decode_ais_message(complete_message, timezone_offset)
            else:
                logging.error(f"Failed to assemble message from {len(fragments)} fragments (seq: {nmea_info['sequence_id']}, channel: {nmea_info['channel']})")
                return None
        else:
            # Still waiting for more fragments
            logging.debug(f"Waiting for fragments: have {len(fragments)}/{nmea_info['total_fragments']} for seq {nmea_info['sequence_id']}, channel {nmea_info['channel']}")
            
            # Cleanup expired fragments periodically
            cleanup_expired_fragments()
            
            return None
            
    except Exception as e:
        logging.error(f"Error processing AIS message with fragments: {e}")
        return None

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
        
        # Determine AIS class based on message type
        if decoded.msg_type in [1, 2, 3, 5]:
            vessel_data['ais_class'] = 'A'
        elif decoded.msg_type in [18, 19, 24]:
            vessel_data['ais_class'] = 'B'
        elif decoded.msg_type in [4, 9, 21]:  # Base stations, SAR aircraft, aids to navigation
            vessel_data['ais_class'] = 'Other'
        else:
            vessel_data['ais_class'] = 'Unknown'
        
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
                        
                    if hasattr(decoded, 'turn') and decoded.turn != -128:
                        if decoded.turn == 127:
                            vessel_data['turn_rate'] = None  # Turn rate not available
                        elif decoded.turn == -127:
                            vessel_data['turn_rate'] = None  # Turn rate not available
                        else:
                            vessel_data['turn_rate'] = decoded.turn * 4.733  # Convert to degrees/minute
                        
                    if hasattr(decoded, 'status'):
                        vessel_data['nav_status'] = decoded.status
                        vessel_data['nav_status_text'] = get_navigation_status(decoded.status)
                        
                    # Report age calculation (time since last position update)
                    if hasattr(decoded, 'second'):
                        current_second = datetime.now(timezone.utc).second
                        if decoded.second <= 59:
                            vessel_data['report_age_seconds'] = abs(current_second - decoded.second)
                        else:
                            vessel_data['report_age_seconds'] = None
            
            # Extract vessel names from Class B Extended reports (Type 19)
            if decoded.msg_type == 19:
                if hasattr(decoded, 'shipname') and decoded.shipname:
                    vessel_data['shipname'] = decoded.shipname.strip('@').strip()
                if hasattr(decoded, 'ship_type'):
                    vessel_data['ship_type'] = decoded.ship_type
                    vessel_data['ship_type_text'] = get_ship_type_name(decoded.ship_type)
                if hasattr(decoded, 'to_bow'):
                    vessel_data['length'] = decoded.to_bow + getattr(decoded, 'to_stern', 0)
                    vessel_data['beam'] = getattr(decoded, 'to_port', 0) + getattr(decoded, 'to_starboard', 0)
        
        # Static data (Type 5)
        elif decoded.msg_type == 5:
            if hasattr(decoded, 'shipname') and decoded.shipname:
                vessel_data['shipname'] = decoded.shipname.strip('@').strip()
            if hasattr(decoded, 'ship_type'):
                vessel_data['ship_type'] = decoded.ship_type
                vessel_data['ship_type_text'] = get_ship_type_name(decoded.ship_type)
            if hasattr(decoded, 'imo'):
                vessel_data['imo_number'] = decoded.imo
            if hasattr(decoded, 'callsign') and decoded.callsign:
                vessel_data['callsign'] = decoded.callsign.strip('@').strip()
            if hasattr(decoded, 'destination') and decoded.destination:
                vessel_data['destination'] = decoded.destination.strip('@').strip()
                
            # ETA parsing
            if hasattr(decoded, 'month') and hasattr(decoded, 'day') and hasattr(decoded, 'hour') and hasattr(decoded, 'minute'):
                if decoded.month != 0 and decoded.day != 0:
                    try:
                        current_year = datetime.now().year
                        eta_datetime = datetime(current_year, decoded.month, decoded.day, 
                                              decoded.hour if decoded.hour != 24 else 0, 
                                              decoded.minute if decoded.minute != 60 else 0)
                        vessel_data['eta_utc'] = eta_datetime.isoformat()
                    except ValueError:
                        vessel_data['eta_utc'] = None
                
            if hasattr(decoded, 'to_bow'):
                vessel_data['length'] = decoded.to_bow + getattr(decoded, 'to_stern', 0)
                vessel_data['beam'] = getattr(decoded, 'to_port', 0) + getattr(decoded, 'to_starboard', 0)
                
            if hasattr(decoded, 'draught') and decoded.draught != 0:
                vessel_data['draught'] = decoded.draught / 10.0
        
        # Class B Static Data Report (Type 24)
        elif decoded.msg_type == 24:
            if hasattr(decoded, 'partno'):
                if decoded.partno == 0 and hasattr(decoded, 'shipname') and decoded.shipname:
                    vessel_data['shipname'] = decoded.shipname.strip('@').strip()
                elif decoded.partno == 1:
                    if hasattr(decoded, 'ship_type'):
                        vessel_data['ship_type'] = decoded.ship_type
                        vessel_data['ship_type_text'] = get_ship_type_name(decoded.ship_type)
                    if hasattr(decoded, 'callsign') and decoded.callsign:
                        vessel_data['callsign'] = decoded.callsign.strip('@').strip()
                    if hasattr(decoded, 'to_bow'):
                        vessel_data['length'] = decoded.to_bow + getattr(decoded, 'to_stern', 0)
                        vessel_data['beam'] = getattr(decoded, 'to_port', 0) + getattr(decoded, 'to_starboard', 0)
        
        # Base Station Report (Type 4) - has position and timestamp
        elif decoded.msg_type == 4:
            if hasattr(decoded, 'lat') and hasattr(decoded, 'lon'):
                if decoded.lat != 91.0 and decoded.lon != 181.0:
                    vessel_data.update({
                        'latitude': decoded.lat,
                        'longitude': decoded.lon,
                    })
        
        # Standard SAR Aircraft Position Report (Type 9)
        elif decoded.msg_type == 9:
            if hasattr(decoded, 'lat') and hasattr(decoded, 'lon'):
                if decoded.lat != 91.0 and decoded.lon != 181.0:
                    vessel_data.update({
                        'latitude': decoded.lat,
                        'longitude': decoded.lon,
                    })
                    if hasattr(decoded, 'speed') and decoded.speed != 1023:
                        vessel_data['speed'] = decoded.speed
                    if hasattr(decoded, 'course') and decoded.course != 3600:
                        vessel_data['course'] = decoded.course / 10.0
        
        return vessel_data
                
    except Exception as e:
        logging.error(f"AIS decode error for message '{raw_message[:50]}...': {e}")
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
                    ais_class TEXT,
                    imo_number INTEGER,
                    callsign TEXT,
                    destination TEXT,
                    eta_utc TEXT,
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
                    turn_rate REAL,
                    range_meters REAL,
                    bearing_degrees REAL,
                    report_age_seconds INTEGER,
                    nav_status INTEGER,
                    nav_status_text TEXT,
                    timestamp TIMESTAMP,
                    timezone_offset INTEGER,
                    FOREIGN KEY (mmsi) REFERENCES vessels(mmsi)
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mmsi_timestamp ON vessel_movements(mmsi, timestamp)')
            
            # Add new columns to existing tables if they don't exist
            try:
                cursor.execute('ALTER TABLE vessels ADD COLUMN ais_class TEXT')
            except sqlite3.OperationalError:
                pass  # Column already exists
            
            try:
                cursor.execute('ALTER TABLE vessels ADD COLUMN imo_number INTEGER')
            except sqlite3.OperationalError:
                pass  # Column already exists
                
            try:
                cursor.execute('ALTER TABLE vessels ADD COLUMN eta_utc TEXT')
            except sqlite3.OperationalError:
                pass  # Column already exists
            
            try:
                cursor.execute('ALTER TABLE vessel_movements ADD COLUMN turn_rate REAL')
            except sqlite3.OperationalError:
                pass  # Column already exists
                
            try:
                cursor.execute('ALTER TABLE vessel_movements ADD COLUMN range_meters REAL')
            except sqlite3.OperationalError:
                pass  # Column already exists
                
            try:
                cursor.execute('ALTER TABLE vessel_movements ADD COLUMN bearing_degrees REAL')
            except sqlite3.OperationalError:
                pass  # Column already exists
                
            try:
                cursor.execute('ALTER TABLE vessel_movements ADD COLUMN report_age_seconds INTEGER')
            except sqlite3.OperationalError:
                pass  # Column already exists
            
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
    """Save vessel data to database (async) with intelligent field preservation"""
    try:
        loop = asyncio.get_event_loop()
        
        def _save_vessel():
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Get existing vessel data from database
            cursor.execute('''
                SELECT mmsi, shipname, ship_type, ship_type_text, ais_class, imo_number,
                       callsign, destination, eta_utc, length, beam, draught, first_seen
                FROM vessels WHERE mmsi = ?
            ''', (mmsi,))
            existing_vessel = cursor.fetchone()
            
            # Helper function to get the best available value
            def get_best_value(key, db_idx=None):
                new_val = vessel_data.get(key)
                
                # If we have a meaningful new value, use it
                if new_val is not None:
                    if isinstance(new_val, str):
                        if new_val.strip():  # Non-empty string
                            return new_val
                    elif isinstance(new_val, (int, float)):
                        if new_val != 0 or key in ['latitude', 'longitude']:  # Allow zero for coordinates
                            return new_val
                    else:
                        if new_val:  # Truthy value
                            return new_val
                
                # Fall back to existing database value if available
                if existing_vessel and db_idx is not None:
                    return existing_vessel[db_idx]
                    
                return new_val  # Return None/empty if no better option
            
            # Prepare vessel static data with field preservation
            first_seen_time = vessel_data.get('first_seen') or vessel_data.get('timestamp')
            if existing_vessel:
                first_seen_time = existing_vessel[12] or first_seen_time  # Use existing first_seen if available
            
            # Update vessel static data
            cursor.execute('''
                INSERT OR REPLACE INTO vessels (
                    mmsi, shipname, ship_type, ship_type_text, ais_class, imo_number,
                    callsign, destination, eta_utc, length, beam, draught, first_seen, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (
                mmsi,
                get_best_value('shipname', 1),
                get_best_value('ship_type', 2),
                get_best_value('ship_type_text', 3),
                get_best_value('ais_class', 4),
                get_best_value('imo_number', 5),
                get_best_value('callsign', 6),
                get_best_value('destination', 7),
                get_best_value('eta_utc', 8),
                get_best_value('length', 9),
                get_best_value('beam', 10),
                get_best_value('draught', 11),
                first_seen_time
            ))
            
            # Always insert movement data if position is available
            # This creates a historical record of all position updates
            if vessel_data.get('latitude') is not None and vessel_data.get('longitude') is not None:
                cursor.execute('''
                    INSERT INTO vessel_movements (
                        mmsi, latitude, longitude, speed, course, heading, turn_rate,
                        range_meters, bearing_degrees, report_age_seconds,
                        nav_status, nav_status_text, timestamp, timezone_offset
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    mmsi,
                    vessel_data.get('latitude'),
                    vessel_data.get('longitude'),
                    vessel_data.get('speed'),
                    vessel_data.get('course'),
                    vessel_data.get('heading'),
                    vessel_data.get('turn_rate'),
                    vessel_data.get('range_meters'),
                    vessel_data.get('bearing_degrees'),
                    vessel_data.get('report_age_seconds'),
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
        logging.error(f"Vessel data: {vessel_data}")

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
    global ais_vessels, websocket_connections, mqtt_client
    
    if not vessel_data or 'mmsi' not in vessel_data:
        logging.debug("Invalid vessel data or missing MMSI")
        return
    
    mmsi = vessel_data['mmsi']
    
    if mmsi not in ais_vessels:
        ais_vessels[mmsi] = {
            'mmsi': mmsi,
            'first_seen': vessel_data['timestamp']
        }
        logging.info(f"New vessel detected: MMSI {mmsi}")
    
    # Calculate range and bearing to other vessels if position is available
    if vessel_data.get('latitude') is not None and vessel_data.get('longitude') is not None:
        try:
            config = get_config()
            lpu_config = config.get('lpu', {})
            reference_lat = lpu_config.get('reference_latitude')
            reference_lon = lpu_config.get('reference_longitude')
            
            # If reference position is configured, calculate range and bearing from reference point
            if reference_lat is not None and reference_lon is not None:
                range_meters, bearing_degrees = calculate_range_bearing(
                    reference_lat, reference_lon,
                    vessel_data['latitude'], vessel_data['longitude']
                )
                vessel_data['range_meters'] = range_meters
                vessel_data['bearing_degrees'] = bearing_degrees
        except Exception as e:
            logging.debug(f"Could not calculate range/bearing for vessel {mmsi}: {e}")
    
    # Update vessel data with intelligent field merging
    # This ensures we keep the latest non-null value for each field
    for key, value in vessel_data.items():
        should_update = False
        
        if key == 'raw_message':
            # Always update raw message
            should_update = True
        elif key in ['timestamp', 'last_seen', 'msg_type', 'timezone_offset']:
            # Always update temporal and metadata fields
            should_update = True
        elif value is not None:
            # For data fields, update if:
            # 1. We have a non-null value, AND
            # 2. Either the field doesn't exist yet, OR the value is not empty/zero
            if isinstance(value, str):
                # For strings, update if not empty after stripping
                if value.strip():
                    should_update = True
            elif isinstance(value, (int, float)):
                # For numbers, update if not zero (except for valid zero values like latitude)
                if value != 0 or key in ['latitude', 'longitude', 'heading', 'course', 'speed']:
                    should_update = True
            else:
                # For other types, update if truthy
                if value:
                    should_update = True
        
        if should_update:
            ais_vessels[mmsi][key] = value
            logging.debug(f"Updated vessel {mmsi} field '{key}' = '{value}'")
        elif key not in ais_vessels[mmsi]:
            # Initialize field if it doesn't exist
            ais_vessels[mmsi][key] = value
    
    ais_vessels[mmsi]['last_seen'] = vessel_data['timestamp']
    
    # Log vessel name updates for debugging
    if vessel_data.get('shipname'):
        logging.info(f"Vessel name update: MMSI {mmsi} = '{vessel_data.get('shipname')}'")
    
    await save_vessel_to_db(mmsi, ais_vessels[mmsi])
    
    # For WebSocket broadcast, ensure we have the most complete vessel data
    # by fetching the latest from database if this was a significant update
    broadcast_data = ais_vessels[mmsi].copy()
    
    # If this update included static data (name, type, etc.), refresh from DB
    # to ensure we have the most complete merged data
    if any(key in vessel_data for key in ['shipname', 'ship_type', 'callsign', 'destination', 'imo_number']):
        try:
            # Get the latest complete vessel data from database
            loop = asyncio.get_event_loop()
            
            def _get_complete_vessel():
                conn = get_db_connection()
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT 
                        v.mmsi, v.shipname, v.ship_type_text, v.ais_class, v.imo_number,
                        v.callsign, v.destination, v.eta_utc, v.length, v.beam, v.draught, 
                        v.first_seen, v.last_updated,
                        m.latitude, m.longitude, m.speed, m.course, m.heading, m.turn_rate,
                        m.range_meters, m.bearing_degrees, m.report_age_seconds,
                        m.nav_status_text, m.timestamp, m.timezone_offset
                    FROM vessels v
                    LEFT JOIN (
                        SELECT mmsi, latitude, longitude, speed, course, heading, turn_rate,
                               range_meters, bearing_degrees, report_age_seconds,
                               nav_status_text, timestamp, timezone_offset,
                               ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY timestamp DESC) as rn
                        FROM vessel_movements
                        WHERE mmsi = ? AND datetime(timestamp) > datetime('now', '-1 hour')
                    ) m ON v.mmsi = m.mmsi AND m.rn = 1
                    WHERE v.mmsi = ?
                ''', (mmsi, mmsi))
                
                row = cursor.fetchone()
                conn.close()
                
                if row:
                    return {
                        'mmsi': row[0], 'shipname': row[1], 'ship_type_text': row[2],
                        'ais_class': row[3], 'imo_number': row[4], 'callsign': row[5], 
                        'destination': row[6], 'eta_utc': row[7], 'length': row[8], 'beam': row[9], 
                        'draught': row[10], 'first_seen': row[11], 'last_updated': row[12],
                        'latitude': row[13], 'longitude': row[14], 'speed': row[15], 'course': row[16], 
                        'heading': row[17], 'turn_rate': row[18], 'range_meters': row[19], 
                        'bearing_degrees': row[20], 'report_age_seconds': row[21],
                        'nav_status_text': row[22], 'timestamp': row[23], 'timezone_offset': row[24]
                    }
                return None
            
            complete_vessel = await loop.run_in_executor(None, _get_complete_vessel)
            if complete_vessel:
                # Update our in-memory vessel with complete data
                for key, value in complete_vessel.items():
                    if value is not None:
                        ais_vessels[mmsi][key] = value
                broadcast_data = ais_vessels[mmsi].copy()
                
        except Exception as e:
            logging.debug(f"Could not refresh complete vessel data for {mmsi}: {e}")
    
    # Broadcast to WebSocket connections
    if websocket_connections:
        await broadcast_vessel_update(broadcast_data)
    
    # Publish to MQTT
    if mqtt_client and mqtt_client.is_connected():
        try:
            config = get_config()
            lpu_id = config.get('lpu', {}).get('id', 'unknown')
            mqtt_payload = {
                "id": lpu_id,
                "msg": vessel_data.get('raw_message', ''),
                "mmsi": mmsi,
                "vessel_name": vessel_data.get('shipname', ''),
                "ais_class": vessel_data.get('ais_class', '')
            }
            ais_json = json.dumps(mqtt_payload)
            mqtt_client.publish("ais/raw", ais_json)
        except Exception as e:
            logging.error(f"Failed to publish AIS to MQTT: {e}")

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
                                vessel_data = process_ais_message_with_fragments(line, timezone_offset)
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
        # Enhanced shutdown sequence
        try:
            # Set a maximum shutdown time to prevent hanging
            await asyncio.wait_for(
                shutdown_handler(),
                timeout=15.0  # Maximum 15 seconds for complete shutdown
            )
            await asyncio.wait_for(
                shutdown_complete.wait(),
                timeout=2.0  # Wait max 2 seconds for shutdown completion
            )
        except asyncio.TimeoutError:
            logging.warning("Shutdown sequence timed out, forcing exit")
        except Exception as e:
            logging.error(f"Error during lifespan shutdown: {e}")
        
        logging.info("Lifespan cleanup completed")

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
                    v.mmsi, v.shipname, v.ship_type_text, v.ais_class, v.imo_number,
                    v.callsign, v.destination, v.eta_utc, v.length, v.beam, v.draught, 
                    v.first_seen, v.last_updated,
                    m.latitude, m.longitude, m.speed, m.course, m.heading, m.turn_rate,
                    m.range_meters, m.bearing_degrees, m.report_age_seconds,
                    m.nav_status_text, m.timestamp, m.timezone_offset
                FROM vessels v
                LEFT JOIN (
                    SELECT mmsi, latitude, longitude, speed, course, heading, turn_rate,
                           range_meters, bearing_degrees, report_age_seconds,
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
                    'ais_class': row[3], 'imo_number': row[4], 'callsign': row[5], 
                    'destination': row[6], 'eta_utc': row[7], 'length': row[8], 'beam': row[9], 
                    'draught': row[10], 'first_seen': row[11], 'last_updated': row[12],
                    'latitude': row[13], 'longitude': row[14], 'speed': row[15], 'course': row[16], 
                    'heading': row[17], 'turn_rate': row[18], 'range_meters': row[19], 
                    'bearing_degrees': row[20], 'report_age_seconds': row[21],
                    'nav_status_text': row[22], 'timestamp': row[23], 'timezone_offset': row[24]
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
        logging.info(f"Main process received signal {signum}")
        stop_event.set()
        
        # Give uvicorn a chance to shutdown gracefully
        import threading
        import time
        
        def delayed_exit():
            time.sleep(2)  # Give 2 seconds for graceful shutdown
            if not shutdown_complete.is_set():
                logging.warning("Graceful shutdown timed out, forcing exit")
                import sys
                sys.exit(0)
        
        # Start delayed exit in background thread
        threading.Thread(target=delayed_exit, daemon=True).start()
    
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