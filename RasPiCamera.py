#!/usr/bin/python

import logging
import optparse, ConfigParser
import time, os, subprocess, tempfile, cStringIO
import threading, Queue
import time
import smtplib


import gdata.photos.service
from PIL import Image
# This limit of a 1000 pics per album comes from here:
# https://support.google.com/picasa/answer/43879?hl=fi
# But it can be any value under 1000.
MAX_PHOTOS_PER_ALBUM = 1000

# Specify the config.ini file
INI_FILE='config.ini'



class BackgroundUpload(threading.Thread):
    """thread that runs in the background uploading pics to Google"""
    def __init__ (self, album_params, q, name_prefix, myname):
        self.album_params = album_params
        self.q = q
        threading.Thread.__init__ (self)
        self.daemon = True
        self.myname = myname
        self.name_prefix = name_prefix


    def check_type(self, filehandle):
        """
        Calling:
        extension = os.path.splitext(filehandle.name)[1][1:]
        would be nice, but won't work on a StringIO buffer. Take the lazy
        option and if it's a StringIO buffer, assume it's a bmp :)
        we could, of course, just use self.myname; but I'd rather use that
        just for debug (and remove self.myname sometime in future.)
        """
        
        if (isinstance(filehandle, cStringIO.OutputType)):
            pic_type='image/bmp'
        else:
            pic_type='image/jpeg'

        return pic_type

    # Creates a new album and updates all the internal references
    def create_next_album(self):
        self.album_params.current_album_suffix += 1
        self.album_params.album_name = self.album_params.unsuffixed_album_name \
                                       + ("_%d" % self.album_params.current_album_suffix)
        self.album_params.album_url, new_album = self.album_params.gdata.create_album(self.album_params.album_name)
        self.album_params.num_photos = 0
    

    def run(self):
        while True:
            logging.debug("%s Wait on queue" % self.myname)
            filehandle = self.q.get()
            logging.debug("%s: popped one off the queue" % self.myname)
            pic_type = self.check_type(filehandle)
            picasa_filename = self.name_prefix + time.strftime(" - %H:%M:%S")
            
            while True:
                try:                    
                    photo = self.album_params.gdata.picasa.InsertPhotoSimple(
                                                          self.album_params.album_url,
                                                          picasa_filename,
                                                          '',
                                                          filehandle,
                                                          content_type=pic_type)
                    logging.debug("%s: Pic uploaded to Google. Photos in album: %d" % (self.myname, (self.album_params.num_photos+1)))
                    filehandle.close()
                    if (os.path.exists(filehandle.name)):
                        os.unlink(filehandle.name)
                    self.q.task_done()
                    self.album_params.num_photos += 1
                    if (self.album_params.num_photos >= MAX_PHOTOS_PER_ALBUM):
                        # limit for the number of photos per album in Google.
                        logging.info("exceeded max number of photos per album. Create new one")
                        self.create_next_album()
                    
                except Exception as ex:
                    logging.critical("InsertPhotoSimple barfed! Exception: %s" % ex)
                    # InsertPhotoSimple appears to die for some users.
                    # my assumption is a momentary break in their internet connection. 
                    # Try re-logging in
                    while (not self.album_params.gdata.login()):
                        logging.error("Re-Login failure. Try again in 5s...")
                        time.sleep(5) # chill awhile
                    continue # this will take us back to InsertPhotoSimple
                break # no exceptions? Great carry on to wait on the queue


class ConfigRead:
    """reads config parameters from config.ini"""
    def __init__(self, filepath):
        config = ConfigParser.ConfigParser()
        config.read(filepath)

        # don't print the LOGIN section!
        logging.debug(config._sections['CONFIG'])
        logging.debug(config._sections['PICTURE'])


        self.email    = config.get('LOGIN','email')
        self.password = config.get('LOGIN','password')
        self.username = config.get('LOGIN','username')
        
        
        self.loop_hrs = config.getint('CONFIG','hrs_to_loop')            
        self.threshold = config.getint('CONFIG','picture_threshold')
        self.sensitivity = config.getint('CONFIG','picture_sensitivity')
        self.forceCapture = config.getboolean('CONFIG','forceCapture')
        self.forceCaptureTime = config.getint('CONFIG','forceCaptureTime')
        self.upload_scratch_pics = config.getboolean('CONFIG','upload_scratch_pics')
        self.scratchImageWidth = config.getint('CONFIG','scratchImageWidth')
        self.scratchImageHeight = config.getint('CONFIG','scratchImageHeight')
        
        
        self.name_prefix = config.get('PICTURE','file_name_prefix')
        self.album_name = config.get('PICTURE','album_name') \
                          + time.strftime(config.get('PICTURE','album_name_suffix'))
        self.rotation = config.getint('PICTURE','camera_rotation')
        self.cam_options = config.get('PICTURE','cam_options')

        try:
            logging.debug(config._sections['selectivescan'])
            # this is fairly horrendous as I'm parsing the ini file
            # and building a list of lists. I'm trying to change as little of the logic as 
            # possible between this implementation and the one posted at:
            # http://www.raspberrypi.org/phpBB3/viewtopic.php?p=391583#p391583
            # Sharing core logic allows us to take further enhancements as people submit them
            # But this is probably better in an external .py module with a variable I can import,
            # so any errors are caught by the interpreter rather than my shaky logic :) TODO
            items = config.items("selectivescan")
            l = []
            self.scanBorders = []
            #for key, points in items:
            for index, values in enumerate(items):
                if (values[0] == 'scratchdebugmode'):
                    # this is part of the same config section, but not relevant
                    # to the list we're building. Just go round again!
                    continue

                points = map(int, values[1].split(','))
                l.append(points)
                if ((len(l) % 2) == 0):
                    # every pair lists generates one area to scan
                    self.scanBorders.append(l)
                    l = []
                    
            self.scanDebugMode = config.getboolean('selectivescan','scratchDebugMode')
            
        except (ConfigParser.NoSectionError, KeyError):
            self.scanBorders = [ [[1,self.scratchImageWidth],[1,self.scratchImageHeight]] ]
            self.scanDebugMode = False
            logging.debug("No selective scan section. Using default scanborders: %s" % \
                                                                       self.scanBorders)
        
        self.scanAreaCount = len(self.scanBorders)
        

# bundled object to store some parameters for the individual threads
class GoogleAlbumParams:
    def __init__(self, gdata, album, num_photos, unsuffixed_album_name, current_album_suffix):
        self.gdata = gdata
        self.album_name = album.title.text
        self.album_url = '/data/feed/api/user/default/albumid/%s' % (album.gphoto_id.text)
        self.num_photos = int(num_photos)
        self.unsuffixed_album_name = unsuffixed_album_name
        self.current_album_suffix = int(current_album_suffix)



class GoogleLogin:
    """This class handles the gdata login + album id extraction gubbins"""
    def __init__(self, email, password, username):
        self.username = username
        self.email = email
        self.password = password
    
    def login(self):
        try:
            self.picasa = gdata.photos.service.PhotosService(email=self.email,
                                                password=self.password)
            self.picasa.ProgrammaticLogin()
            return True
        except Exception as ex:
            logging.critical("Google Login failed! Exception: %s" % ex)
            return False
    
    def get_album_url(self, album_name):
        logging.debug('get_album_url for: %s' % album_name)
        albums = self.picasa.GetUserFeed(user=self.username)
        
        search_again = True
        album_url = None
        temp_album_name = album_name + "_0"
        album_suffix = 1
        logging.debug('Searching for album %s' % temp_album_name)
        
        while search_again:
          search_again = False
          logging.debug('Album Search. Suffix is %s' % (album_suffix-1))
          for album in albums.entry:
            if album.title.text==temp_album_name:
              # Lets make sure this album is not full....
              num_photos = int(album.numphotos.text)
              if (num_photos >= MAX_PHOTOS_PER_ALBUM):
                # OK... the album is full. We need to create a new album
                # or maybe we've done this before. Lets spin around to recheck all albums
                temp_album_name = album_name + ("_%d" % album_suffix)
                album_suffix += 1
                search_again = True
                logging.debug('%d pics in this album. Switch to %s  ' % (num_photos, temp_album_name))
              else:
                # the album has < MAX_PHOTOS_PER_ALBUM pics in it. It'll do for now!
                logging.debug('Selecting album %s! It has %d pictures in it' % (temp_album_name, num_photos))
                album_url = '/data/feed/api/user/default/albumid/%s' % (album.gphoto_id.text)

              # either way we have a match. No point going through the rest of the albums
              # without restarting from the top (if required)
              break
        
        # if the album does not exist, create it!    
        if (album_url == None):
            logging.info('Album does not exist.')
            album_url, album = self.create_album(temp_album_name)
            num_photos = 0

        return album, num_photos, album_name, (album_suffix-1)
        
    def create_album(self, album_name):
        logging.info('Creating Album: %s ' % album_name)
        try:
            album = self.picasa.InsertAlbum(title=album_name, summary="",access='private')
            album_url = '/data/feed/api/user/default/albumid/%s' % (album.gphoto_id.text)
        except GooglePhotosException as gpe:
            logging.critical("Album creation failed!")
            sys.exit(gpe.message)
        
        return album_url, album


# Capture a small test image (for motion detection)
def capture_test_image(config):
    command = "raspistill -rot %s -w %s -h %s -t 0 -e bmp -o -" % \
              (config.rotation, config.scratchImageWidth, config.scratchImageHeight)
    # StringIO used here as to not wear out the SD card
    # There will be a lot of these pics taken
    imageData = cStringIO.StringIO()
    imageData.write(subprocess.check_output(command, shell=True))
    imageData.seek(0)
    im = Image.open(imageData)
    buffer = im.load()
    return buffer, imageData

# capture full-size image and add it to the queue for background upload
def upload_image(queue, config):
    command = "raspistill -rot %s -w IMAGE_WIDTH -h IMAGE_HEIGTH -e jpg %s -o -" % \
                            (config.rotation, config.cam_options)
        
    """
    These files are >1.5Mb in size and the upload happens in a background thread. 
    We can't get the background thread to take the picture as only one thread can
    access the camera. So we take the picture here and send just the handle to the 
    background thread.
    Using a StringIO here (like above, to save disk writes) would be a bad idea for
    two reasons:
    (a) Now we need to send the entire StringIO buffer via the queue
        Large memory consumption (compared to the 100x75 pics above)
    (b) The upload takes a lot of time and if there is lots of motion, there
        will be lots of items in the queue. Again, allowing the memory 
        consumption to spiral out of control
    
    So, use a tempfile, which does write to disk, but it auto cleans up/deletes
    after the filehandle goes out of scope
    """
    temp_file = tempfile.NamedTemporaryFile(suffix='.jpg')
    temp_file.write(subprocess.check_output(command, shell=True))
    temp_file.seek(0)
    logging.debug("hirez queue push")
    queue.put(temp_file)
    


# sets up default logging levels based on command line parameters
# based on code from:
# http://web.archive.org/web/20120819135307/http://aymanh.com/python-debugging-techniques

LOGGING_LEVELS = {'critical': logging.CRITICAL,
                  'error': logging.ERROR,
                  'warning': logging.WARNING,
                  'info': logging.INFO,
                  'debug': logging.DEBUG}
                  
def loglvl_setup():
    parser = optparse.OptionParser()
    parser.add_option('-l', '--logging-level', help='Logging level')
    parser.add_option('-f', '--logging-file', help='Logging file name')
    (options, args) = parser.parse_args()
    logging_level = LOGGING_LEVELS.get(options.logging_level, logging.WARNING)
    logging.basicConfig(level=logging_level, filename=options.logging_file,
                  format='%(asctime)s %(levelname)s: %(message)s',
                  datefmt='%Y-%m-%d %H:%M:%S')


def keep_looping(end_time):
    if (end_time == 0):
        return True
    else:
        return (time.time() < end_time)

def main():
    loglvl_setup()
    
    logging.debug("Starting up....")
    # config.ini should be in the same location as the script
    # get script path with some os.path hackery

    # check if config.ini does exist
    if not ( os.path.exists(INI_FILE)):
        print "ERROR: config.ini does not exist...exiting"
        return 0

    current_path = os.path.dirname(os.path.realpath(__file__))
    config = ConfigRead(os.path.join(current_path,INI_FILE))

    if (config.loop_hrs == 0):
        end_time = 0
    else:
        end_time = time.time() + (config.loop_hrs*60*60)

    logging.debug("Login to Google")
    gdata_login = GoogleLogin(config.email, config.password, config.username)
    while (not gdata_login.login()):
        time.sleep(0.5) # chill awhile


    album, num_photos, unsuffixed_album_name, current_album_suffix = gdata_login.get_album_url(config.album_name)
    album_params = GoogleAlbumParams(gdata_login, album, num_photos, unsuffixed_album_name, current_album_suffix)

    
    logging.debug("Setup Threads & Queues")
    upload_queue = Queue.Queue()
    uploadThread = BackgroundUpload(album_params,
                                     upload_queue,
                                     config.name_prefix, 
                                     "FullUploader")
    uploadThread.start()
    
    # do we need to upload the 100x75 thumbnails too?
    # If so spawn another thread + queue to handle that
    if (config.upload_scratch_pics):
        album, num_photos, unsuffixed_album_name, current_album_suffix = gdata_login.get_album_url(config.album_name + "_thumbs")
        album_params_thumbs = GoogleAlbumParams(
                    gdata_login, album, num_photos, unsuffixed_album_name, current_album_suffix)

        upload_queue_thumbs = Queue.Queue()
        uploadThread_thumbs = BackgroundUpload(album_params_thumbs, 
                                                upload_queue_thumbs,
                                                config.name_prefix, 
                                                "ThumbUploader")
        uploadThread_thumbs.start()
    else :
        # This just helps with logging logic later in the code
        upload_queue_thumbs = 0
        

    #get an image to kick the process off with
    buffer1, file_handle = capture_test_image(config)

    # Reset last capture time
    lastCapture = time.time()

    # main loop
    # original motion detection code from 
    # http://www.raspberrypi.org/phpBB3/viewtopic.php?p=358259#p362915
    # with updates from
    # http://www.raspberrypi.org/phpBB3/viewtopic.php?p=391583#p391583
    # TODO: requires cleanup
    logging.debug("Main Loop start")
    while (keep_looping(end_time)):
        # Get comparison image
        logging.debug("Current queue size FullSize:%d ThumbSize:%d" % \
                                   (upload_queue.qsize(), 
                                   (upload_queue_thumbs.qsize() if upload_queue_thumbs else 0) ))
        buffer2, file_handle = capture_test_image(config)
        
        # Count changed pixels
        changedPixels = 0
        takePicture = False
        
        if (config.scanDebugMode): # in debug mode, save a bitmap-file with marked changed pixels and with visible testarea-borders
            debugimage = Image.new("RGB",(config.scratchImageWidth, config.scratchImageHeight))
            debugim = debugimage.load()

        for z in xrange(0, config.scanAreaCount): # = xrange(0,1) with default-values = z will only have the value of 0 = only one scan-area = whole picture
            for x in xrange(config.scanBorders[z][0][0]-1, config.scanBorders[z][0][1]): # = xrange(0,100) with default-values
                for y in xrange(config.scanBorders[z][1][0]-1, config.scanBorders[z][1][1]):   # = xrange(0,75) with default-values
                    if (config.scanDebugMode):
                        debugim[x,y] = buffer2[x,y]
                        if ((x == config.scanBorders[z][0][0]-1) 
                                             or (x == config.scanBorders[z][0][1]-1)
                                             or (y == config.scanBorders[z][1][0]-1)
                                             or (y == config.scanBorders[z][1][1]-1)):
                            #logging.debug( "Border %s %s" % (x,y))
                            debugim[x,y] = (0, 0, 255) # in debug mode, mark all border pixel to blue
                    # Just check green channel as it's the highest quality channel
                    pixdiff = abs(buffer1[x,y][1] - buffer2[x,y][1])
                    if pixdiff > config.threshold:
                        changedPixels += 1
                        if (config.scanDebugMode):
                            debugim[x,y] = (0, 255, 0) # in debug mode, mark all changed pixel to green
                    # Save an image if pixels changed
                    if (changedPixels > config.sensitivity):
                        takePicture = True # will shoot the main photo later
                    if ((config.scanDebugMode == False) and (changedPixels > config.sensitivity)):
                        break  # break the y loop
                if ((config.scanDebugMode == False) and (changedPixels > config.sensitivity)):
                    break  # break the x loop
            if ((config.scanDebugMode == False) and (changedPixels > config.sensitivity)):
                break  # break the z loop

        
        if (config.scanDebugMode):
            debugimage.save(current_path + "/debug.bmp") # save debug image as bmp
            logging.debug("debug.bmp saved, %s changed pixels" % changedPixels)
            
        # Check force capture
        if config.forceCapture:
            if time.time() - lastCapture > config.forceCaptureTime:
                logging.debug("it's been %s seconds since last pic taken. Force it!" % (time.time() - lastCapture))
                takePicture = True

        if takePicture:
            lastCapture = time.time()
            # Take a full size picture and farm it off for background upload
            upload_image(upload_queue, config)

            if (config.upload_scratch_pics):
                logging.debug("low rez queue push")
                """
                Note that we're going to dump the entire StringIO buffer 
                into the queue for upload. As the picture size is 100x75, 
                the upload's happen quickly; stopping the queue 
                from consuming too much memory
                """
                upload_queue_thumbs.put(file_handle)
                
                # as upload_scratch_pics is True, we upload *all* scratch pics
                if (config.scanDebugMode):
                    # need to convert it to a StringIO as the gdata client needs
                    # something resembling a proper filehandle
                    debugSIO = cStringIO.StringIO()
                    debugimage.save(debugSIO, "BMP")
                    upload_queue_thumbs.put(debugSIO)


        # Swap comparison buffers
        buffer1 = buffer2

    # The script has run for hrs_to_loop hours. Time to quit.
    logging.info("Wait until all pictures are uploaded")
    upload_queue.join()
    
    logging.debug("Exiting.....")

def sendEmail():
  username = "xxxxxx"
  password = "xxxxxx"
  FROM = "jasebell@gmail.com"
  TO = ["jasebell@gmail.com"]
  SUBJECT = "Testing sending using gmail"
  TEXT = "Motion was detected from the Raspberry Pi"

  message = """\From: %s\nTo: %s\nSubject: %s\n\n%s
  """ % (FROM, ", ".join(TO), SUBJECT, TEXT)
  try:
    server = smtplib.SMTP("smtp.gmail.com", 587) #or port 465 doesn't seem to work!
    server.ehlo()
    server.starttls()
    server.login(username, password)
    server.sendmail(FROM, TO, message)
    server.close()
    print 'successfully sent the mail'
  except:
    print "failed to send mail"

if __name__ == '__main__':
    main()
