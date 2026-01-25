from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Game, Match, Player, ScheduledMatch


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


class PlayerRegistrationForm(UserCreationForm):
    """User registration + player profile creation"""
    
    email = forms.EmailField(required=True)
    full_name = forms.CharField(max_length=100, label="Full Name")
    nickname = forms.CharField(max_length=50, required=False)
    playing_style = forms.ChoiceField(
        choices=[
            ('normal', 'Normal'),
            ('hard_rubber', 'Hard Rubber'),
            ('unknown', 'Unknown'),
        ],
        initial='normal',
    )
    
    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']
    
    def save(self, commit=True):
        user: User = super().save(commit=False)
        user.email = self.cleaned_data['email']
        
        if commit:
            user.save()
            Player.objects.create(
                user=user,
                name=self.cleaned_data['full_name'],
                nickname=self.cleaned_data.get('nickname', ''),
                playing_style=self.cleaned_data['playing_style']
            )

        return user


class ScheduledMatchForm(forms.ModelForm):
    """Form for scheduling a future match"""

    class Meta:
        model = ScheduledMatch
        fields = ['player1', 'player2', 'scheduled_date', 'scheduled_time', 'location', 'notes']
        widgets = {
            'scheduled_date': forms.DateInput(attrs={
                'type': 'date',
                'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'
            }),
            'scheduled_time': forms.TimeInput(attrs={
                'type': 'time',
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
            'notes': forms.Textarea(attrs={
                'class': 'flex min-h-[80px] w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm',
                'rows': 3,
                'placeholder': 'Any additional notes about the scheduled match...'
            }),
        }

    def clean(self):
        """Validate the form"""
        cleaned_data = super().clean()
        player1 = cleaned_data.get('player1')
        player2 = cleaned_data.get('player2')
        scheduled_date = cleaned_data.get('scheduled_date')

        # Ensure players are different
        if player1 and player2 and player1 == player2:
            raise forms.ValidationError("Player 1 and Player 2 must be different!")

        # Ensure date is in the future
        if scheduled_date:
            from django.utils import timezone
            today = timezone.now().date()
            if scheduled_date < today:
                raise forms.ValidationError("Scheduled date must be today or in the future!")

        return cleaned_data
