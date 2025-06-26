#!/usr/bin/env python3
import asyncio
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

class TCPStreamer:
    def __init__(self, csv_file: str, host: str, port: int):
        self.csv_file = csv_file
        self.host = host
        self.port = port
        self.server = None
        self.clients = set()
        self.running = False
        
    async def start(self):
        try:
            self.server = await asyncio.start_server(
                self.handle_client,
                self.host,
                self.port
            )
            self.running = True
            logging.info(f"Started TCP server on {self.host}:{self.port} streaming {self.csv_file}")
            
            async with self.server:
                await self.server.serve_forever()
                
        except Exception as e:
            logging.error(f"Error starting server: {e}")
    
    async def handle_client(self, reader, writer):
        client_addr = writer.get_extra_info('peername')
        logging.info(f"Client {client_addr} connected")
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
            logging.info(f"Client {client_addr} disconnected")
    
    async def stream_data(self):
        while self.running:
            try:
                with open(self.csv_file, 'r') as file:
                    for line in file:
                        if not self.running:
                            break
                        line = line.strip()
                        if line and self.clients:
                            await self.broadcast_message(line)
                        await asyncio.sleep(0.5)
            except FileNotFoundError:
                logging.error(f"File {self.csv_file} not found")
                break
            except Exception as e:
                logging.error(f"Error reading file: {e}")
                break
    
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
    
    async def run(self):
        # Start server first
        asyncio.create_task(self.start())
        await asyncio.sleep(1)  # Give server time to start
        
        # Then start streaming
        try:
            await self.stream_data()
        except KeyboardInterrupt:
            logging.info("Shutting down...")
        finally:
            self.running = False
            if self.server:
                self.server.close()
                await self.server.wait_closed()

async def main():
    if len(sys.argv) != 4:
        print("Usage: python3 tcpraw.py <csv_file> <ip_address> <port>")
        print("Example: python3 tcpraw.py aisraw.csv 0.0.0.0 20008")
        sys.exit(1)
    
    csv_file = sys.argv[1]
    host = sys.argv[2]
    port = int(sys.argv[3])
    
    streamer = TCPStreamer(csv_file, host, port)
    await streamer.run()

if __name__ == '__main__':
    asyncio.run(main())