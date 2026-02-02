from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Game, Match, Player, ScheduledMatch, Team


class MatchForm(forms.ModelForm):
    player1 = forms.ModelChoiceField(
    queryset=Player.objects.all(),
    required=True,
    widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )
    player2 = forms.ModelChoiceField(
        queryset=Player.objects.all(),
        required=True,
        widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )
    player3 = forms.ModelChoiceField(
        queryset=Player.objects.all(),
        required=False,  # Optional for singles
        widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )
    player4 = forms.ModelChoiceField(
        queryset=Player.objects.all(),
        required=False,  # Optional for singles
        widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )
    class Meta:
        model = Match
        fields = ['is_double', 'date_played', 'location', 'match_type', 'best_of', 'notes']
        widgets = {
            'is_double' : forms.Select(choices=[
                (False, 'Single'),
                (True, 'Double')],
                attrs={
                    'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'}
            ),
            'date_played': forms.DateTimeInput(attrs={
                'type': 'datetime-local',
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
        player3 = cleaned_data.get('player3')
        player4 = cleaned_data.get('player4')

        # Ensure players are different
        players = [player1, player2, player3, player4]
        players = [player for player in players if player]
        if len(players) != len(set(players)):
            raise forms.ValidationError("All players must be different!")

        # Two players for singles
        if not cleaned_data.get('is_double') and len(players) > 2:
            raise forms.ValidationError("Only two players are required for a doubles match!")
        # Four players for doubles
        if cleaned_data.get('is_double') and len(players) != 4:
            raise forms.ValidationError("Four players are required for a doubles match!")
            
        return cleaned_data


class MatchEditForm(forms.ModelForm):
    """Form for editing completed matches - only location and notes"""
    class Meta:
        model = Match
        fields = ['location', 'notes']


class TeamEditForm(forms.ModelForm):
    """Form for editing completed teams - only name"""
    class Meta:
        model = Team
        fields = ['name']


class GameForm(forms.ModelForm):
    class Meta:
        model = Game
        fields = ['game_number', 'team1_score', 'team2_score', 'duration_minutes']
    
    def clean(self):
        cleaned_data = super().clean()
        t1_score = cleaned_data.get('team1_score')
        t2_score = cleaned_data.get('team2_score')
        
        if t1_score is not None and t2_score is not None:
            if t1_score == t2_score:
                raise forms.ValidationError("A game cannot end in a tie!")
            
            # Standard table tennis rules: must win by 2 at 10-10
            if t1_score >= 10 and t2_score >= 10:
                if abs(t1_score - t2_score) < 2:
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

    player1 = forms.ModelChoiceField(
    queryset=Player.objects.all(),
    required=True,
    widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )
    player2 = forms.ModelChoiceField(
        queryset=Player.objects.all(),
        required=True,
        widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )

    class Meta:
        model = ScheduledMatch
        fields = ['scheduled_date', 'scheduled_time', 'location', 'notes']
        widgets = {
            'scheduled_date': forms.DateInput(attrs={
                'type': 'date',
                'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'
            }),
            'scheduled_time': forms.TimeInput(attrs={
                'type': 'time',
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


class MatchConvertForm(forms.ModelForm):
    """Form for converting scheduled matches to played matches"""
    player1 = forms.ModelChoiceField(
        queryset=Player.objects.all(),
        required=True,
        widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )
    player2 = forms.ModelChoiceField(
        queryset=Player.objects.all(),
        required=True,
        widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )
    player3 = forms.ModelChoiceField(
        queryset=Player.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )
    player4 = forms.ModelChoiceField(
        queryset=Player.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'})
    )

    class Meta:
        model = Match
        fields = ['is_double', 'date_played', 'location', 'match_type', 'best_of', 'notes']
        widgets = {
            'is_double': forms.Select(
                choices=[(False, 'Single'), (True, 'Double')],
                attrs={'class': 'flex h-12 w-full rounded-md border border-input bg-white px-3 py-2 text-base md:text-sm'}
            ),
            'date_played': forms.DateTimeInput(attrs={
                'type': 'datetime-local',
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

    def __init__(self, *args, **kwargs):
        scheduled_match = kwargs.pop('scheduled_match', None)
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if scheduled_match:
            # Pre-fill from scheduled match
            from datetime import datetime
            from django.utils import timezone

            # Get players from teams
            team1_players = list(scheduled_match.team1.players.all())
            team2_players = list(scheduled_match.team2.players.all())

            # Set is_double based on team sizes
            is_double = len(team1_players) == 2 and len(team2_players) == 2
            self.initial['is_double'] = is_double

            # Set players
            if team1_players:
                self.initial['player1'] = team1_players[0]
                if len(team1_players) > 1:
                    self.initial['player3'] = team1_players[1]

            if team2_players:
                self.initial['player2'] = team2_players[0]
                if len(team2_players) > 1:
                    self.initial['player4'] = team2_players[1]

            # Combine scheduled_date and scheduled_time into date_played
            scheduled_datetime = datetime.combine(
                scheduled_match.scheduled_date,
                scheduled_match.scheduled_time
            )
            # Make it timezone-aware
            scheduled_datetime = timezone.make_aware(scheduled_datetime)
            self.initial['date_played'] = scheduled_datetime

            # Pre-fill location and notes
            if scheduled_match.location:
                self.initial['location'] = scheduled_match.location
            if scheduled_match.notes:
                self.initial['notes'] = scheduled_match.notes

            # Lock player fields for non-staff users
            if user and not user.is_staff:
                for field_name in ['player1', 'player2', 'player3', 'player4', 'is_double']:
                    self.fields[field_name].disabled = True
                    self.fields[field_name].help_text = "Players are locked based on the scheduled match"

    def clean(self):
        """Validate player uniqueness and singles/doubles requirements"""
        cleaned_data = super().clean()
        player1 = cleaned_data.get('player1')
        player2 = cleaned_data.get('player2')
        player3 = cleaned_data.get('player3')
        player4 = cleaned_data.get('player4')

        # Ensure players are different
        players = [player1, player2, player3, player4]
        players = [player for player in players if player]
        if len(players) != len(set(players)):
            raise forms.ValidationError("All players must be different!")

        # Two players for singles
        if not cleaned_data.get('is_double') and len(players) > 2:
            raise forms.ValidationError("Only two players are required for a singles match!")

        # Four players for doubles
        if cleaned_data.get('is_double') and len(players) != 4:
            raise forms.ValidationError("Four players are required for a doubles match!")

        return cleaned_data
