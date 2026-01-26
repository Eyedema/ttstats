from django.contrib import admin
from .models import Player, Match, Game, Location, UserProfile, ScheduledMatch

admin.site.register(Player)
admin.site.register(Match)
admin.site.register(Game)
admin.site.register(Location)
admin.site.register(UserProfile)
admin.site.register(ScheduledMatch)