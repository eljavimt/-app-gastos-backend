import sys
import os

# Añadir el directorio actual y su carpeta app al path de Python
sys.path.insert(0, os.path.dirname(__file__))

try:
    from a2wsgi import ASGIMiddleware
    from app.main import app as fastapi_app
    application = ASGIMiddleware(fastapi_app)
except Exception as e:
    # Retornar un error simple si faltan dependencias por instalar en Hostinger
    def application(environ, start_response):
        start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
        return [f"Error de inicializacion de Passenger: {str(e)}\nAsegurate de instalar requirements.txt en Hostinger.".encode('utf-8')]
