#!/usr/bin/env python3

import pygame
import dbus
import dbus.mainloop.glib
import requests
import io
import math
import threading
import time
import os
from PIL import Image
from gi.repository import GLib

# Display dimensions
WIDTH, HEIGHT = 480, 272

class GPSMapDisplay:
    def __init__(self):
        # Initialize PyGame
        pygame.init()
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT))
        pygame.display.set_caption("GPS Map Display")
        self.clock = pygame.time.Clock()
        
        # Load fonts
        self.font_small = pygame.font.SysFont('Arial', 10)
        self.font_medium = pygame.font.SysFont('Arial', 12)
        
        # Map variables
        self.current_location = None
        self.map_center = (-0.787166, 51.617864)  # Default center
        self.zoom = 16
        self.tile_size = 256
        self.tiles = {}  # Dictionary to store multiple tiles: {(x, y): tile_surface}
        self.current_tile_coords = None  # Center tile coordinates
        
        # Calculate how many tiles we need to cover the display
        self.tiles_x = math.ceil(WIDTH / self.tile_size) + 2  # +2 for buffer
        self.tiles_y = math.ceil(HEIGHT / self.tile_size) + 2  # +2 for buffer
        
        # Tile cache setup
        self.cache_dir = "map_cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # Tile download settings
        self.edge_buffer_percent = 0.20  # 20% buffer for smoother panning
        self.edge_buffer_pixels = int(self.tile_size * self.edge_buffer_percent)
        self.last_tile_download_time = 0
        self.download_cooldown = 1.0  # seconds between tile downloads
        
        # Colors
        self.colors = {
            'background': (40, 40, 40),
            'text': (255, 255, 255),
            'marker': (0, 120, 255),
            'accuracy': (255, 100, 100, 100),
            'status_ok': (50, 200, 50),
            'status_warn': (200, 200, 50),
        }
        
        # Load initial map tiles
        self.load_map_tiles()
        
        # Start D-Bus listener in separate thread
        self.running = True
        self.dbus_thread = threading.Thread(target=self.start_dbus_listener)
        self.dbus_thread.daemon = True
        self.dbus_thread.start()
        
    def lon2tile(self, lon, zoom):
        """Convert longitude to tile number"""
        return int((lon + 180.0) / 360.0 * (2 ** zoom))
    
    def lat2tile(self, lat, zoom):
        """Convert latitude to tile number"""
        return int((1.0 - math.log(math.tan(lat * math.pi / 180.0) + 
                  1.0 / math.cos(lat * math.pi / 180.0)) / math.pi) / 
                  2.0 * (2 ** zoom))
    
    def tile2lon(self, x, zoom):
        """Convert tile number to longitude"""
        return x / (2 ** zoom) * 360.0 - 180.0
    
    def tile2lat(self, y, zoom):
        """Convert tile number to latitude"""
        n = math.pi - 2.0 * math.pi * y / (2 ** zoom)
        return 180.0 / math.pi * math.atan(0.5 * (math.exp(n) - math.exp(-n)))
    
    def get_tile_filename(self, x, y, zoom):
        """Generate cache filename for tile"""
        return os.path.join(self.cache_dir, f"tile_{zoom}_{x}_{y}.png")
    
    def load_cached_tile(self, x, y, zoom):
        """Load tile from cache if available"""
        filename = self.get_tile_filename(x, y, zoom)
        if os.path.exists(filename):
            try:
                return pygame.image.load(filename)
            except:
                return None
        return None
    
    def save_tile_to_cache(self, x, y, zoom, tile_data):
        """Save tile to cache"""
        filename = self.get_tile_filename(x, y, zoom)
        try:
            pygame.image.save(tile_data, filename)
        except:
            pass
    
    def create_fallback_tile(self, x, y):
        """Create a fallback tile for missing tiles"""
        tile = pygame.Surface((self.tile_size, self.tile_size))
        tile.fill((80, 80, 80))
        
        # Draw grid
        for i in range(0, self.tile_size, 32):
            pygame.draw.line(tile, (120, 120, 120), (i, 0), (i, self.tile_size), 1)
            pygame.draw.line(tile, (120, 120, 120), (0, i), (self.tile_size, i), 1)
        
        # Add tile coordinates
        font = pygame.font.SysFont('Arial', 12)
        text = font.render(f"{x},{y}", True, (180, 180, 180))
        tile.blit(text, (10, 10))
        
        return tile
    
    def download_tile(self, x, y, zoom):
        """Download a single tile"""
        try:
            url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
            
            # Add headers to be polite to OSM servers
            headers = {
                'User-Agent': 'i.MX6-GPS-Display/1.0 (embedded navigation system)'
            }
            
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                # Convert to PyGame surface
                image_data = io.BytesIO(response.content)
                pil_image = Image.open(image_data)
                rgb_image = pil_image.convert('RGB')
                data = rgb_image.tobytes()
                
                tile = pygame.image.fromstring(data, rgb_image.size, rgb_image.mode)
                self.save_tile_to_cache(x, y, zoom, tile)
                return tile
            else:
                print(f"❌ Failed to download tile {x},{y}: HTTP {response.status_code}")
                return self.create_fallback_tile(x, y)
                
        except Exception as e:
            print(f"❌ Error downloading tile {x},{y}: {e}")
            return self.create_fallback_tile(x, y)
    
    def get_tiles_to_load(self, center_x, center_y):
        """Get all tile coordinates needed to fill the display"""
        tiles_to_load = []
        
        # Calculate how many tiles we need in each direction
        tiles_horizontal = math.ceil(WIDTH / self.tile_size) + 1
        tiles_vertical = math.ceil(HEIGHT / self.tile_size) + 1
        
        # Calculate starting tile coordinates
        start_x = center_x - tiles_horizontal // 2
        start_y = center_y - tiles_vertical // 2
        
        # Generate all tile coordinates needed
        for dx in range(tiles_horizontal + 1):
            for dy in range(tiles_vertical + 1):
                tile_x = start_x + dx
                tile_y = start_y + dy
                tiles_to_load.append((tile_x, tile_y))
        
        return tiles_to_load
    
    def load_map_tiles(self):
        """Load all tiles needed to fill the display"""
        try:
            current_time = time.time()
            
            # Check download cooldown
            if current_time - self.last_tile_download_time < self.download_cooldown:
                return
                
            lon, lat = self.map_center
            
            # Calculate center tile coordinates
            center_x = self.lon2tile(lon, self.zoom)
            center_y = self.lat2tile(lat, self.zoom)
            
            # Check if we need to load new tiles
            if (self.current_tile_coords and 
                self.current_tile_coords == (center_x, center_y, self.zoom)):
                return  # Same center tile, no need to reload
                
            self.current_tile_coords = (center_x, center_y, self.zoom)
            self.last_tile_download_time = current_time
            
            print(f"🔄 Loading tiles for center: {center_x},{center_y}@{self.zoom}")
            
            # Get all tiles needed to fill the display
            tiles_to_load = self.get_tiles_to_load(center_x, center_y)
            
            # Load or download each tile
            new_tiles = {}
            for x, y in tiles_to_load:
                # Try to load from cache first
                cached_tile = self.load_cached_tile(x, y, self.zoom)
                if cached_tile:
                    new_tiles[(x, y)] = cached_tile
                else:
                    # Download new tile
                    tile = self.download_tile(x, y, self.zoom)
                    new_tiles[(x, y)] = tile
                    time.sleep(0.05)  # Be nice to OSM servers
            
            self.tiles = new_tiles
            print(f"✅ Loaded {len(self.tiles)} tiles for display")
            
        except Exception as e:
            print(f"❌ Error loading map tiles: {e}")
    
    def start_dbus_listener(self):
        """Start listening for GPSD signals on D-Bus"""
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()
        
        try:
            bus.add_signal_receiver(
                self.on_gpsd_fix,
                dbus_interface="org.gpsd",
                signal_name="fix",
                path="/org/gpsd"
            )
            
            print("📡 GPSD listener started")
            
            # Start GLib main loop
            loop = GLib.MainLoop()
            loop.run()
            
        except Exception as e:
            print(f"❌ D-Bus error: {e}")
    
    def on_gpsd_fix(self, *args):
        """Callback for GPSD fix signals"""
        try:
            if len(args) != 15:
                return
            
            # Parse GPS data
            lat = float(args[3])
            lon = float(args[4])
            h_accuracy = float(args[5]) if not math.isnan(float(args[5])) else None
            altitude = float(args[6]) if not math.isnan(float(args[6])) else None
            speed = float(args[10]) if not math.isnan(float(args[10])) else None
            mode = int(args[1])
            
            self.current_location = {
                'latitude': lat,
                'longitude': lon,
                'altitude': altitude,
                'accuracy': h_accuracy,
                'speed': speed,
                'mode': mode,
                'timestamp': time.time()
            }
            
            # Update map center to current position
            old_center = self.map_center
            self.map_center = (lon, lat)
            
            # Calculate tile coordinates
            center_x = self.lon2tile(lon, self.zoom)
            center_y = self.lat2tile(lat, self.zoom)
            
            # Check if we moved to a different tile or near edge
            old_center_x = self.lon2tile(old_center[0], self.zoom) if old_center else None
            old_center_y = self.lat2tile(old_center[1], self.zoom) if old_center else None
            
            if (old_center_x != center_x or old_center_y != center_y or 
                not self.current_tile_coords):
                self.load_map_tiles()
            
            # Print update occasionally
            if time.time() % 5 < 0.1:
                print(f"📍 GPS: {lat:.6f}, {lon:.6f} - Tile: {center_x},{center_y}")
            
        except Exception as e:
            print(f"❌ Error processing GPS: {e}")
    
    def draw_map(self):
        """Draw all tiles to fill the display"""
        if not self.tiles or not self.current_tile_coords:
            return
            
        center_x, center_y, zoom = self.current_tile_coords
        lon, lat = self.map_center
        
        # Calculate pixel position within center tile
        pixel_x = int((lon - self.tile2lon(center_x, zoom)) * 
                     self.tile_size / (self.tile2lon(center_x + 1, zoom) - 
                     self.tile2lon(center_x, zoom)))
        
        pixel_y = int((lat - self.tile2lat(center_y, zoom)) * 
                     self.tile_size / (self.tile2lat(center_y + 1, zoom) - 
                     self.tile2lat(center_y, zoom)))
        
        # Calculate offset to center the map on current position
        offset_x = WIDTH // 2 - pixel_x
        offset_y = HEIGHT // 2 - pixel_y
        
        # Draw all tiles
        for (tile_x, tile_y), tile_surface in self.tiles.items():
            # Calculate position of this tile
            tile_screen_x = offset_x + (tile_x - center_x) * self.tile_size
            tile_screen_y = offset_y + (tile_y - center_y) * self.tile_size
            
            # Only draw if tile is visible on screen
            if (tile_screen_x + self.tile_size > 0 and tile_screen_x < WIDTH and
                tile_screen_y + self.tile_size > 0 and tile_screen_y < HEIGHT):
                self.screen.blit(tile_surface, (tile_screen_x, tile_screen_y))
    
    def draw_marker(self):
        """Draw the position marker"""
        if self.current_location:
            # Always draw marker at center of screen
            center_x, center_y = WIDTH // 2, HEIGHT // 2
            
            # Draw accuracy circle if available
            if self.current_location['accuracy']:
                acc_px = min(int(self.current_location['accuracy'] * 3), 100)
                pygame.draw.circle(self.screen, (255, 0, 0, 100), 
                                 (center_x, center_y), acc_px, 1)
            
            # Draw blue marker
            pygame.draw.circle(self.screen, self.colors['marker'], (center_x, center_y), 8)
            pygame.draw.circle(self.screen, (255, 255, 255), (center_x, center_y), 3)
    
    def draw_info_panel(self):
        """Draw the information overlay"""
        if not self.current_location:
            return
        
        # Create semi-transparent background
        panel = pygame.Surface((200, 80), pygame.SRCALPHA)
        panel.fill((0, 0, 0, 180))
        self.screen.blit(panel, (10, 10))
        
        # Display coordinates and info
        lat = self.current_location['latitude']
        lon = self.current_location['longitude']
        alt = self.current_location['altitude'] or 0
        acc = self.current_location['accuracy'] or 0
        spd = (self.current_location['speed'] or 0) * 3.6  # Convert to km/h
        
        texts = [
            f"Lat: {lat:.6f}",
            f"Lon: {lon:.6f}", 
            f"Alt: {alt:.0f}m  Acc: {acc:.0f}m",
            f"Spd: {spd:.1f}km/h  Zoom: {self.zoom}"
        ]
        
        for i, text in enumerate(texts):
            text_surface = self.font_small.render(text, True, self.colors['text'])
            self.screen.blit(text_surface, (15, 15 + i * 15))
    
    def draw_status_bar(self):
        """Draw status bar at bottom"""
        status_rect = pygame.Rect(0, HEIGHT - 20, WIDTH, 20)
        pygame.draw.rect(self.screen, (0, 0, 0, 180), status_rect)
        
        if self.current_location and self.current_location['mode'] > 0:
            status_text = "GPS Locked - 3D Fix"
            status_color = self.colors['status_ok']
            time_text = time.strftime("%H:%M:%S")
        else:
            status_text = "Searching for GPS..."
            status_color = self.colors['status_warn']
            time_text = "--:--:--"
        
        # Show tile count
        tile_text = f"Tiles: {len(self.tiles)}"
        
        status_surface = self.font_medium.render(status_text, True, status_color)
        time_surface = self.font_small.render(time_text, True, (200, 200, 200))
        tile_surface = self.font_small.render(tile_text, True, (200, 200, 200))
        
        self.screen.blit(status_surface, (10, HEIGHT - 16))
        self.screen.blit(time_surface, (WIDTH - 80, HEIGHT - 16))
        self.screen.blit(tile_surface, (WIDTH // 2 - 30, HEIGHT - 16))
    
    def run(self):
        """Main display loop"""
        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        self.running = False
                    elif event.key == pygame.K_PLUS or event.key == pygame.K_EQUALS:
                        self.zoom = min(self.zoom + 1, 18)
                        self.load_map_tiles()  # Reload all tiles on zoom change
                    elif event.key == pygame.K_MINUS:
                        self.zoom = max(self.zoom - 1, 2)
                        self.load_map_tiles()  # Reload all tiles on zoom change
            
            # Clear screen
            self.screen.fill(self.colors['background'])
            
            # Draw map, marker, and info
            self.draw_map()
            self.draw_marker()
            self.draw_info_panel()
            self.draw_status_bar()
            
            # Update display
            pygame.display.flip()
            self.clock.tick(10)  # 10 FPS
        
        pygame.quit()

def main():
    try:
        display = GPSMapDisplay()
        display.run()
    except Exception as e:
        print(f"❌ Failed to start display: {e}")
        pygame.quit()

if __name__ == "__main__":
    main()