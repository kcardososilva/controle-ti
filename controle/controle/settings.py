"""
Django settings for controle project.

Credenciais sensíveis devem ser definidas via variáveis de ambiente.
Crie um arquivo .env na raiz do projeto com as chaves abaixo
(veja .env.example para o modelo).
"""

from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Carrega variáveis do arquivo .env (se existir)
load_dotenv(BASE_DIR / ".env")

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError(
        "DJANGO_SECRET_KEY não definida. "
        "Adicione-a ao arquivo .env e reinicie o servidor."
    )

DEBUG = os.environ.get('DJANGO_DEBUG', 'False') == 'True'

ALLOWED_HOSTS = os.environ.get(
    'DJANGO_ALLOWED_HOSTS',
    '127.0.0.1,localhost'
).split(',')

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'ProjetoEstoque',
    'about',
    'users',
    'widget_tweaks',
    'django.contrib.humanize',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serve os arquivos estaticos (CSS/JS/imagens) de forma
    # comprimida e com cache no navegador, sem passar pela stack do Django.
    # Deve vir logo apos o SecurityMiddleware.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'ProjetoEstoque.middleware.LoginThrottleMiddleware',
    'ProjetoEstoque.middleware.TVAccessMiddleware',
    'ProjetoEstoque.middleware.FornecedorAccessMiddleware',
]

ROOT_URLCONF = 'controle.urls'

# Loaders de template:
#  • Produção (DEBUG=False) → cached.Loader: cada template é compilado UMA vez e
#    reusado da memória. Como as páginas têm muito CSS inline, isso elimina o
#    re-parse a cada requisição (ganho grande de render).
#  • Dev (DEBUG=True) → loaders simples, para o auto-reload de template continuar.
# APP_DIRS e 'loaders' são mutuamente exclusivos: por isso APP_DIRS sai e a lista
# de loaders assume o mesmo papel — filesystem (DIRS) + app_directories
# (templates dos apps, incluindo o admin).
_TEMPLATE_LOADERS = [
    'django.template.loaders.filesystem.Loader',
    'django.template.loaders.app_directories.Loader',
]
if not DEBUG:
    _TEMPLATE_LOADERS = [('django.template.loaders.cached.Loader', _TEMPLATE_LOADERS)]

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
            'loaders': _TEMPLATE_LOADERS,
        },
    },
]

WSGI_APPLICATION = 'controle.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        'OPTIONS': {
            'timeout': 20,
        },
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'pt-br'

TIME_ZONE = 'America/Sao_Paulo'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Armazenamento de arquivos: estaticos servidos via WhiteNoise (comprimidos +
# cache). Sem manifesto, para nao quebrar referencias estaticas existentes.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.outlook.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
ALERTA_EMAIL = os.environ.get('ALERTA_EMAIL', '')
ALERTA_EMAILS = [
    e.strip()
    for e in os.environ.get('ALERTA_EMAILS', '').split(',')
    if e.strip()
]


DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'login'

# Cache em memória — mais rápido que FileBasedCache para servidor único local
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "TIMEOUT": 300,
    }
}

# PRTG Network Monitor — credenciais via .env
PRTG_URL      = os.environ.get("PRTG_URL", "")
PRTG_USER     = os.environ.get("PRTG_USER", "")
PRTG_PASSHASH = os.environ.get("PRTG_PASSHASH", "")

# NinjaOne RMM — credenciais via .env (OAuth2 Client Credentials)
# Gerar em: Administração → Apps → API → Adicionar aplicativo OAuth
NINJA_BASE_URL      = os.environ.get("NINJA_BASE_URL", "")
NINJA_CLIENT_ID     = os.environ.get("NINJA_CLIENT_ID", "")
NINJA_CLIENT_SECRET = os.environ.get("NINJA_CLIENT_SECRET", "")
# URI exata cadastrada no app OAuth do NinjaOne — deve corresponder exatamente
NINJA_REDIRECT_URI  = os.environ.get(
    "NINJA_REDIRECT_URI",
    "http://santa-colomba-karitel-qqprmnjdwc.dynamic-m.com:65300/ninja/oauth/callback/",
)
