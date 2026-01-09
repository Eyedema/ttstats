from django import forms
from .models import Match, Game, Player


class MatchForm(forms.ModelForm):
    class Meta:
        model = Match
        fields = ['player1', 'player2', 'date_played', 'location', 'match_type', 'best_of', 'notes']
        widgets = {
            'date_played': forms.DateTimeInput(attrs={
                'type': 'datetime-local',
                'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'
            }),
            'player1': forms.Select(attrs={
                'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'
            }),
            'player2': forms.Select(attrs={
                'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'
            }),
            'location': forms.Select(attrs={
                'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'
            }),
            'match_type': forms.Select(attrs={
                'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'
            }),
            'best_of': forms.NumberInput(attrs={
                'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm',
                'min': 1,
                'max': 11,
                'step': 2
            }),
            'notes': forms.Textarea(attrs={
                'class': 'flex min-h-[80px] w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm',
                'rows': 3
            }),
        }
    
    def clean(self):
        """Additional validation"""
        cleaned_data = super().clean()
        player1 = cleaned_data.get('player1')
        player2 = cleaned_data.get('player2')
        
        # Ensure players are different
        if player1 and player2 and player1 == player2:
            raise forms.ValidationError("Player 1 and Player 2 must be different!")
        
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
