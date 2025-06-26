#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import csv
import random
import math
import time
import logging
from typing import List
import signal
import sys

from pyais.encode import encode_dict

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

class VesselData:
    """Represents a vessel with its static and dynamic data"""
    
    def __init__(self, client: str, port: int, name: str, mmsi: int, vessel_type: str, 
                 initial_lat: float, initial_lon: float):
        self.port = port
        self.name = name
        self.mmsi = mmsi
        self.vessel_type = vessel_type
        self.current_lat = initial_lat
        self.current_lon = initial_lon
        self.speed = random.uniform(1.0, 3.0)
        self.course = random.uniform(0, 360)
        self.heading = self.course + random.uniform(-1.5, 1.5)
        self.last_update = time.time()

class NMEAGenerator:
    """Generates NMEA AIVDM messages for AIS using pyais library"""
    
    @staticmethod
    def get_ship_type_code(vessel_type: str) -> int:
        return {'tugboat': 52, 'cargo': 70, 'tanker': 80}.get(vessel_type.lower(), 52)
    
    @classmethod
    def create_position_message(cls, vessel: VesselData) -> str:
        try:
            data = {
                'type': 1,
                'mmsi': vessel.mmsi,
                'status': 0,
                'turn': 0,
                'speed': round(vessel.speed * 10),
                'accuracy': 1,
                'lon': vessel.current_lon,
                'lat': vessel.current_lat,
                'course': round(vessel.course * 10),
                'heading': int(vessel.heading),
                'second': int(time.time()) % 60,
                'maneuver': 0,
                'spare': 0,
                'raim': 0,
                'radio': 0
            }
            return encode_dict(data, radio_channel="A", talker_id="AIVDM")[0]
        except:
            return ""
    
    @classmethod
    def create_static_message(cls, vessel: VesselData) -> List[str]:
        try:
            data = {
                'msg_type': 5,
                'mmsi': vessel.mmsi,
                'imo': 0,
                'callsign': vessel.name[:7].upper(),
                'shipname': vessel.name[:20],
                'ship_type': cls.get_ship_type_code(vessel.vessel_type),
                'to_bow': 15,
                'to_stern': 5,
                'to_port': 3,
                'to_starboard': 3,
                'draught': 3.0,
                'destination': 'WORKING',
                'ais_version': 0,
                'month': 0,
                'day': 0,
                'hour': 24,
                'minute': 60,
                'dte': 1,
                'spare': 0
            }
            return encode_dict(data, radio_channel="A", talker_id="AIVDM")
        except:
            return []

class VesselSimulator:
    @staticmethod
    def update_vessel_position(vessel: VesselData, time_delta: float):
        # Simple linear movement
        distance_nm = vessel.speed * (time_delta / 3600.0)
        distance_deg = distance_nm / 60.0
        
        course_rad = math.radians(vessel.course)
        vessel.current_lat += distance_deg * math.cos(course_rad)
        vessel.current_lon += distance_deg * math.sin(course_rad) / math.cos(math.radians(vessel.current_lat))
        
        # Slight course changes
        if random.random() < 0.1:
            vessel.course += random.uniform(-5, 5)
            vessel.course = vessel.course % 360
        
        vessel.heading = vessel.course + random.uniform(-1.5, 1.5)
        vessel.heading = vessel.heading % 360

class TCPServer:
    """TCP server for streaming AIS data to a specific port"""
    
    def __init__(self, port: int, vessels: List[VesselData]):
        self.port = port
        self.vessels = vessels  # Multiple vessels can use same port
        self.server = None
        self.clients = set()
        self.running = False
        
    async def start(self):
        """Start the TCP server"""
        try:
            self.server = await asyncio.start_server(
                self.handle_client,
                '0.0.0.0',
                self.port
            )
            self.running = True
            vessel_names = [v.name for v in self.vessels]
            logging.info(f"Started AIS server on port {self.port} for {len(self.vessels)} vessels: {', '.join(vessel_names)}")
            
            async with self.server:
                await self.server.serve_forever()
                
        except Exception as e:
            logging.error(f"Error starting server on port {self.port}: {e}")
    
    async def handle_client(self, reader, writer):
        client_addr = writer.get_extra_info('peername')
        logging.info(f"Client {client_addr} connected to port {self.port}")
        self.clients.add(writer)
        
        try:
            while self.running:
                try:
                    data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
                    if not data:
                        break
                except asyncio.TimeoutError:
                    pass
        except:
            pass
        finally:
            self.clients.discard(writer)
            writer.close()
            await writer.wait_closed()
    
    async def broadcast_message(self, message: str):
        if not self.clients:
            return
        message_bytes = (message + '\r\n').encode('ascii')
        for writer in list(self.clients):
            try:
                writer.write(message_bytes)
                await writer.drain()
            except:
                self.clients.discard(writer)
    
    async def stop(self):
        """Stop the TCP server"""
        self.running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()

class AISFaker:
    """Main AIS faker application"""
    
    def __init__(self, csv_file: str):
        self.csv_file = csv_file
        self.vessels: List[VesselData] = []
        self.servers: List[TCPServer] = []
        self.running = False
        
    def load_vessels(self):
        with open(self.csv_file, 'r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                if row['client']:
                    vessel = VesselData(
                        client=row['client'],
                        port=int(row['port']),
                        name=row['name'],
                        mmsi=int(row['mmsi']),
                        vessel_type=row['type'],
                        initial_lat=float(row['latitude']),
                        initial_lon=float(row['longitude'])
                    )
                    self.vessels.append(vessel)
                    logging.info(f"Loaded: {vessel.name} on port {vessel.port}")
    
    async def start_servers(self):
        port_vessels = {}
        for vessel in self.vessels:
            if vessel.port not in port_vessels:
                port_vessels[vessel.port] = []
            port_vessels[vessel.port].append(vessel)
        
        for port, vessels in port_vessels.items():
            server = TCPServer(port, vessels)
            self.servers.append(server)
            asyncio.create_task(server.start())
        
        await asyncio.sleep(1)
    
    async def stop_servers(self):
        """Stop all TCP servers"""
        for server in self.servers:
            await server.stop()
    
    async def simulation_loop(self):
        last_static_send = {}
        
        # Send initial static messages
        for vessel in self.vessels:
            server = next((s for s in self.servers if s.port == vessel.port), None)
            if server:
                static_messages = NMEAGenerator.create_static_message(vessel)
                for msg in static_messages:
                    await server.broadcast_message(msg)
                    await asyncio.sleep(0.1)
                last_static_send[vessel.mmsi] = time.time()
        
        while self.running:
            current_time = time.time()
            
            for vessel in self.vessels:
                server = next((s for s in self.servers if s.port == vessel.port), None)
                if not server:
                    continue
                
                # Update position
                time_delta = current_time - vessel.last_update
                VesselSimulator.update_vessel_position(vessel, time_delta)
                vessel.last_update = current_time
                
                # Send position message
                position_msg = NMEAGenerator.create_position_message(vessel)
                if position_msg:
                    await server.broadcast_message(position_msg)
                
                # Send static message every 30 seconds
                if current_time - last_static_send.get(vessel.mmsi, 0) > 30:
                    static_messages = NMEAGenerator.create_static_message(vessel)
                    for msg in static_messages:
                        await server.broadcast_message(msg)
                        await asyncio.sleep(0.1)
                    last_static_send[vessel.mmsi] = current_time
            
            await asyncio.sleep(5)
    
    async def start(self):
        self.running = True
        self.load_vessels()
        
        if not self.vessels:
            return
        
        await self.start_servers()
        
        logging.info(f"Started {len(self.vessels)} vessels")
        ports = list(set(v.port for v in self.vessels))
        for port in ports:
            vessels_on_port = [v.name for v in self.vessels if v.port == port]
            logging.info(f"Port {port}: {', '.join(vessels_on_port)}")
        
        try:
            await self.simulation_loop()
        except KeyboardInterrupt:
            pass
        finally:
            await self.stop()
    
    async def stop(self):
        """Stop the AIS faker"""
        self.running = False
        logging.info("Stopping AIS faker...")
        await self.stop_servers()
        logging.info("AIS faker stopped")

async def main():
    faker = AISFaker('vessel.csv')
    await faker.start()

if __name__ == '__main__':
    asyncio.run(main())