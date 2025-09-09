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
        self.map_center = (-0.787166, 51.617864)  # Default center (your coordinates)
        self.zoom = 16
        self.map_tile = None
        self.tile_size = 256
        self.last_tile_coords = None  # (x, y, zoom)
        
        # Tile cache setup
        self.cache_dir = "map_cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # Tile download settings
        self.edge_buffer_percent = 0.10  # 10% buffer at edges
        self.edge_buffer_pixels = int(self.tile_size * self.edge_buffer_percent)
        self.last_tile_download_time = 0
        self.download_cooldown = 2.0  # seconds between tile downloads
        
        # Colors
        self.colors = {
            'background': (40, 40, 40),
            'text': (255, 255, 255),
            'marker': (0, 120, 255),
            'accuracy': (255, 100, 100, 100),
            'status_ok': (50, 200, 50),
            'status_warn': (200, 200, 50),
        }
        
        # Load initial map tile
        self.load_map_tile()
        
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
    
    def need_new_tile(self, new_x, new_y, new_lon, new_lat):
        """Determine if we need to download a new tile based on position"""
        if self.last_tile_coords is None:
            return True
            
        current_x, current_y, current_zoom = self.last_tile_coords
        
        # If zoom level changed or different tile coordinates, need new tile
        if current_zoom != self.zoom or current_x != new_x or current_y != new_y:
            return True
            
        # Calculate current pixel position within the current tile
        current_pixel_x = int((new_lon - self.tile2lon(current_x, self.zoom)) * 
                             self.tile_size / (self.tile2lon(current_x + 1, self.zoom) - 
                             self.tile2lon(current_x, self.zoom)))
        
        current_pixel_y = int((new_lat - self.tile2lat(current_y, self.zoom)) * 
                             self.tile_size / (self.tile2lat(current_y + 1, self.zoom) - 
                             self.tile2lat(current_y, self.zoom)))
        
        # Check if we're too close to any edge (within 10% buffer zone)
        near_left_edge = current_pixel_x < self.edge_buffer_pixels
        near_right_edge = current_pixel_x > self.tile_size - self.edge_buffer_pixels
        near_top_edge = current_pixel_y < self.edge_buffer_pixels
        near_bottom_edge = current_pixel_y > self.tile_size - self.edge_buffer_pixels
        
        # If approaching any edge, we need to consider downloading adjacent tiles
        return near_left_edge or near_right_edge or near_top_edge or near_bottom_edge
    
    def create_fallback_tile(self):
        """Create a fallback tile when online loading fails"""
        self.map_tile = pygame.Surface((self.tile_size, self.tile_size))
        self.map_tile.fill((100, 100, 100))
        
        # Draw grid
        for i in range(0, self.tile_size, 32):
            pygame.draw.line(self.map_tile, (150, 150, 150), (i, 0), (i, self.tile_size), 1)
            pygame.draw.line(self.map_tile, (150, 150, 150), (0, i), (self.tile_size, i), 1)
        
        # Draw crosshair
        pygame.draw.line(self.map_tile, (200, 200, 200), 
                        (self.tile_size//2, 0), (self.tile_size//2, self.tile_size), 2)
        pygame.draw.line(self.map_tile, (200, 200, 200), 
                        (0, self.tile_size//2), (self.tile_size, self.tile_size//2), 2)
        
        # Add "No Tile" text
        font = pygame.font.SysFont('Arial', 20)
        text = font.render("No Map Data", True, (200, 200, 200))
        self.map_tile.blit(text, (self.tile_size//2 - 60, self.tile_size//2 - 10))
    
    def load_map_tile(self):
        """Load map tile with smart downloading - only when needed"""
        try:
            current_time = time.time()
            
            # Check download cooldown
            if current_time - self.last_tile_download_time < self.download_cooldown:
                return
                
            lon, lat = self.map_center
            
            # Calculate tile coordinates
            x = self.lon2tile(lon, self.zoom)
            y = self.lat2tile(lat, self.zoom)
            
            # Check if we need a new tile
            need_new_tile = self.need_new_tile(x, y, lon, lat)
            
            if not need_new_tile and self.map_tile is not None:
                # Current tile is still good, no need to download
                return
                
            self.last_tile_coords = (x, y, self.zoom)
            
            # Try to load from cache first
            cached_tile = self.load_cached_tile(x, y, self.zoom)
            if cached_tile:
                self.map_tile = cached_tile
                print(f"✅ Using cached tile: {x},{y}@{self.zoom}")
                return
                
            # Download new tile
            print(f"📡 Downloading new tile: {x},{y}@{self.zoom}")
            self.last_tile_download_time = current_time
            
            url = f"https://tile.openstreetmap.org/{self.zoom}/{x}/{y}.png"
            
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
                mode = rgb_image.mode
                size = rgb_image.size
                data = rgb_image.tobytes()
                
                self.map_tile = pygame.image.fromstring(data, size, mode)
                
                # Save to cache
                self.save_tile_to_cache(x, y, self.zoom, self.map_tile)
                
                print(f"✅ Tile downloaded and cached")
                
            else:
                print(f"❌ Failed to download tile: HTTP {response.status_code}")
                self.create_fallback_tile()
                
        except Exception as e:
            print(f"❌ Error loading map tile: {e}")
            self.create_fallback_tile()
    
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
            
            # Parse GPS data according to GPSD documentation
            timestamp = args[0]
            mode = args[1]
            time_uncertainty = args[2]
            lat = args[3]
            lon = args[4]
            h_accuracy = args[5]
            altitude = args[6]
            v_accuracy = args[7]
            course = args[8]
            course_accuracy = args[9]
            speed = args[10]
            speed_accuracy = args[11]
            climb = args[12]
            climb_accuracy = args[13]
            device = args[14]
            
            # Convert NaN values to None
            def safe_float(value):
                try:
                    float_val = float(value)
                    if math.isnan(float_val):
                        return None
                    return float_val
                except (ValueError, TypeError):
                    return None
            
            new_location = {
                'latitude': safe_float(lat),
                'longitude': safe_float(lon),
                'altitude': safe_float(altitude),
                'accuracy': safe_float(h_accuracy),
                'vertical_accuracy': safe_float(v_accuracy),
                'speed': safe_float(speed),
                'course': safe_float(course),
                'climb_rate': safe_float(climb),
                'mode': int(mode),
                'device': str(device),
                'timestamp': float(timestamp)
            }
            
            # Only update if we have valid coordinates
            if new_location['latitude'] and new_location['longitude']:
                self.current_location = new_location
                
                # Update map center to current position
                old_center = self.map_center
                self.map_center = (new_location['longitude'], new_location['latitude'])
                
                # Check if we need to load a new map tile
                self.load_map_tile()
                
                # Print update (throttled)
                if time.time() % 5 < 0.1:  # Print every ~5 seconds
                    print(f"📍 GPS: {new_location['latitude']:.6f}, {new_location['longitude']:.6f}")
            
        except Exception as e:
            print(f"❌ Error processing GPS fix: {e}")
    
    def draw_map(self):
        """Draw the map tile centered on current position"""
        if self.map_tile:
            # Calculate position to center the tile on current location
            tile_x = self.lon2tile(self.map_center[0], self.zoom)
            tile_y = self.lat2tile(self.map_center[1], self.zoom)
            
            # Calculate pixel position within tile
            pixel_x = int((self.map_center[0] - self.tile2lon(tile_x, self.zoom)) * 
                         self.tile_size / (self.tile2lon(tile_x + 1, self.zoom) - 
                         self.tile2lon(tile_x, self.zoom)))
            
            pixel_y = int((self.map_center[1] - self.tile2lat(tile_y, self.zoom)) * 
                         self.tile_size / (self.tile2lat(tile_y + 1, self.zoom) - 
                         self.tile2lat(tile_y, self.zoom)))
            
            # Calculate blit position to center the current location
            blit_x = WIDTH // 2 - pixel_x
            blit_y = HEIGHT // 2 - pixel_y
            
            # Draw the map tile
            self.screen.blit(self.map_tile, (blit_x, blit_y))
    
    def draw_marker(self):
        """Draw the position marker"""
        if self.current_location:
            # Draw a blue dot at the center of the screen
            center_x, center_y = WIDTH // 2, HEIGHT // 2
            
            # Draw accuracy circle if available
            if self.current_location['accuracy']:
                accuracy_pixels = int(self.current_location['accuracy'] * 3)  # Scale for visibility
                accuracy_pixels = min(accuracy_pixels, 100)  # Cap the size
                pygame.draw.circle(self.screen, (255, 0, 0, 100), 
                                 (center_x, center_y), accuracy_pixels, 1)
            
            # Draw blue marker
            pygame.draw.circle(self.screen, self.colors['marker'], (center_x, center_y), 8)
            pygame.draw.circle(self.screen, (255, 255, 255), (center_x, center_y), 3)
    
    def draw_info_panel(self):
        """Draw the information overlay"""
        if not self.current_location:
            return
        
        # Create semi-transparent background
        panel = pygame.Surface((180, 90), pygame.SRCALPHA)
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
        
        status_surface = self.font_medium.render(status_text, True, status_color)
        time_surface = self.font_small.render(time_text, True, (200, 200, 200))
        
        self.screen.blit(status_surface, (10, HEIGHT - 16))
        self.screen.blit(time_surface, (WIDTH - 60, HEIGHT - 16))
    
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
                        self.load_map_tile()  # Force reload on zoom change
                    elif event.key == pygame.K_MINUS:
                        self.zoom = max(self.zoom - 1, 2)
                        self.load_map_tile()  # Force reload on zoom change
            
            # Clear screen
            self.screen.fill(self.colors['background'])
            
            # Draw map, marker, and info
            self.draw_map()
            self.draw_marker()
            self.draw_info_panel()
            self.draw_status_bar()
            
            # Update display
            pygame.display.flip()
            self.clock.tick(10)  # 10 FPS is sufficient for map display
        
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