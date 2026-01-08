from django.contrib import admin
from .models import Player, Match, Game, Location

admin.site.register(Player)
admin.site.register(Match)
admin.site.register(Game)
admin.site.register(Location)