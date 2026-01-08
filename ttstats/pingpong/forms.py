from django import forms
from .models import Match, Game, Player

class MatchForm(forms.ModelForm):
    class Meta:
        model = Match
        fields = ['player1', 'player2', 'date_played', 'location', 'match_type', 'best_of']
    
    def clean(self):
        cleaned_data = super().clean()
        player1 = cleaned_data.get('player1')
        player2 = cleaned_data.get('player2')
        
        if player1 and player2 and player1 == player2:
            raise forms.ValidationError("A player cannot play against themselves!")
        
        return cleaned_data


class MatchEditForm(forms.ModelForm):
    """Form for editing completed matches - only location and notes"""
    class Meta:
        model = Match
        fields = ['location', 'notes']


class GameForm(forms.ModelForm):
    class Meta:
        model = Game
        fields = ['game_number', 'player1_score', 'player2_score', 'duration_minutes']
    
    def clean(self):
        cleaned_data = super().clean()
        p1_score = cleaned_data.get('player1_score')
        p2_score = cleaned_data.get('player2_score')
        
        if p1_score is not None and p2_score is not None:
            if p1_score == p2_score:
                raise forms.ValidationError("A game cannot end in a tie!")
            
            # Standard table tennis rules: must win by 2 at 10-10
            if p1_score >= 10 and p2_score >= 10:
                if abs(p1_score - p2_score) < 2:
                    raise forms.ValidationError("When score is 10-10 or higher, you must win by 2 points!")
        
        return cleaned_data
