'''
Uses convolutional neural network with sliding window to predict if a pixel is part of a ROI
This script predicts labels on test data and outputs ROIs to a json file

Arguments:
Path to training data numpy arrays generated by preprocessing.py
Path to test data numpy arrays generated by preprocessing.py

Outputs:
submission.json with ROIs in json format
'''

import numpy as np
import theano
import theano.tensor as T
from theano.tensor.nnet import conv2d
from theano.tensor.signal import downsample
from sklearn.preprocessing import StandardScaler
import sys
import glob
import random
from skimage.measure import label
import json

# verify the required arguments are given
if (len(sys.argv) < 3):
    print 'Usage: python conv2d_predict.py <PATH_TO_PREPROCESSED_TRAIN_DATA> <PATH_TO_PREPROCESSED_TEST_DATA>'
    exit(1)

#load data
X_train_files = sorted(glob.glob(sys.argv[1] + '/X_neurofinder*'))
y_train_files = sorted(glob.glob(sys.argv[1] + '/y_neurofinder*'))
X_test_files = sorted(glob.glob(sys.argv[1] + '/X_neurofinder*'))

X_train = []
y_train = []
X_test = []

for i in range(len(X_train_files)):
    array = np.load(X_train_files[i])
    mask = np.load(y_train_files[i])
    
    #if image is not 512x512, pad with zeros
    if array.shape != (512,512):
        hdiff = 512 - array.shape[0]
        vdiff = 512 - array.shape[1]
        array = np.pad(array,((0,hdiff),(0,vdiff)),'constant')
        mask = np.pad(mask,((0,hdiff),(0,vdiff)),'constant')
    X_train.append(array)
    y_train.append(mask)

for i in range(len(X_test_files)):
    array = np.load(X_test_files[i])
    #if image is not 512x512, pad with zeros
    if array.shape != (512,512):
        hdiff = 512 - array.shape[0]
        vdiff = 512 - array.shape[1]
        array = np.pad(array,((0,hdiff),(0,vdiff)),'constant')
    X_test.append(array)

#standard scaler
scaler = StandardScaler()
X_temp = np.concatenate((np.array(X_train),np.array(X_test)))
X_flattened = scaler.fit_transform(X_temp.reshape(X_temp.shape[0],X_temp.shape[1]*X_temp.shape[2]))
X_train = X_flattened[:len(X_train),:].reshape(len(X_train),X_temp.shape[1],X_temp.shape[2])
X_test = X_flattened[len(X_train):,:].reshape(len(X_test),X_temp.shape[1],X_temp.shape[2])

#pad by 16 to allow for testing of edge pixels
X_train = np.pad(X_train,((0,0),(20,20),(20,20)),'constant')
y_train = np.pad(y_train,((0,0),(20,20),(20,20)),'constant')
X_test = np.pad(X_test,((0,0),(20,20),(20,20)),'constant')

#get coordinates of all pixels that are part of a ROI and all pixels that are not part of a ROI
#used during training to ensure batches are split evenly between positive and negative samples
nonzeros_train = np.nonzero(y_train)
zeros_train = np.where(y_train[:,20:532,20:532]==0)
for i in zeros_train[1:]:
    i += 20
nonzero_coords_train = zip(nonzeros_train[0],nonzeros_train[1],nonzeros_train[2])
zero_coords_train = zip(zeros_train[0],zeros_train[1],zeros_train[2])

#conv net settings
convolutional_layers = 6
feature_maps = [1,50,50,50,100,100,100]
filter_shapes = [(3,3),(3,3),(3,3),(3,3),(3,3),(3,3)]
feedforward_layers = 1
feedforward_nodes = [2000]
classes = 400

#neural network classes
class convolutional_layer(object):
    '''
    convolutional layer with orthogonal weight init and optional maxpool
    '''
    def __init__(self, input, output_maps, input_maps, filter_height, filter_width, maxpool=None):
        self.input = input
        self.w = theano.shared(self.ortho_weights(output_maps,input_maps,filter_height,filter_width),borrow=True)
        self.b = theano.shared(np.zeros((output_maps,), dtype=theano.config.floatX),borrow=True)
        self.conv_out = conv2d(input=self.input, filters=self.w, border_mode='half')
        if maxpool:
            self.conv_out = downsample.max_pool_2d(self.conv_out, ds=maxpool, ignore_border=True)
        self.output = T.nnet.elu(self.conv_out + self.b.dimshuffle('x', 0, 'x', 'x'))
    def ortho_weights(self,chan_out,chan_in,filter_h,filter_w):
        bound = np.sqrt(6./(chan_in*filter_h*filter_w + chan_out*filter_h*filter_w))
        W = np.random.random((chan_out, chan_in * filter_h * filter_w))
        u, s, v = np.linalg.svd(W,full_matrices=False)
        if u.shape[0] != u.shape[1]:
            W = u.reshape((chan_out, chan_in, filter_h, filter_w))
        else:
            W = v.reshape((chan_out, chan_in, filter_h, filter_w))
        return W.astype(theano.config.floatX)
    def get_params(self):
        return self.w,self.b

class feedforward_layer(object):
    '''
    feedforward layer with orthogonal weight init
    '''
    def __init__(self,input,features,nodes):
        self.input = input
        self.bound = np.sqrt(1.5/(features+nodes))
        self.w = theano.shared(self.ortho_weights(features,nodes),borrow=True)
        self.b = theano.shared(np.zeros((nodes,), dtype=theano.config.floatX),borrow=True)
        self.output = T.nnet.sigmoid(-T.dot(self.input,self.w)-self.b)
    def ortho_weights(self,fan_in,fan_out):
        bound = np.sqrt(2./(fan_in+fan_out))
        W = np.random.randn(fan_in,fan_out)*bound
        u, s, v = np.linalg.svd(W,full_matrices=False)
        if u.shape[0] != u.shape[1]:
            W = u
        else:
            W = v
        return W.astype(theano.config.floatX)
    def get_params(self):
        return self.w,self.b        

class neural_network(object):
    '''
    convolutional neural network with adam gradient descent
    '''
    def __init__(self,convolutional_layers,feature_maps,filter_shapes,feedforward_layers,feedforward_nodes,classes):
        self.input = T.tensor4()        
        self.convolutional_layers = []
        self.convolutional_layers.append(convolutional_layer(self.input,feature_maps[1],feature_maps[0],filter_shapes[0][0],filter_shapes[0][1]))
        for i in range(1,convolutional_layers):
            if i==3:
                self.convolutional_layers.append(convolutional_layer(self.convolutional_layers[i-1].output,feature_maps[i+1],feature_maps[i],filter_shapes[i][0],filter_shapes[i][1],maxpool=(2,2)))
            else:
                self.convolutional_layers.append(convolutional_layer(self.convolutional_layers[i-1].output,feature_maps[i+1],feature_maps[i],filter_shapes[i][0],filter_shapes[i][1]))
        self.feedforward_layers = []
        self.feedforward_layers.append(feedforward_layer(self.convolutional_layers[-1].output.flatten(2),40000,feedforward_nodes[0]))
        for i in range(1,feedforward_layers):
            self.feedforward_layers.append(feedforward_layer(self.feedforward_layers[i-1].output,feedforward_nodes[i-1],feedforward_nodes[i]))
        self.output_layer = feedforward_layer(self.feedforward_layers[-1].output,feedforward_nodes[-1],classes)
        self.params = []
        for l in self.convolutional_layers + self.feedforward_layers:
            self.params.extend(l.get_params())
        self.params.extend(self.output_layer.get_params())
        self.target = T.matrix()
        self.output = self.output_layer.output
        self.cost = -self.target*T.log(self.output)-(1-self.target)*T.log(1-self.output)
        self.cost = self.cost.mean()
        self.updates = self.adam(self.cost, self.params)
        self.propogate = theano.function([self.input,self.target],self.cost,updates=self.updates,allow_input_downcast=True)
        self.classify = theano.function([self.input],self.output,allow_input_downcast=True)
        
    def adam(self, cost, params, lr=0.0002, b1=0.1, b2=0.01, e=1e-8):
        updates = []
        grads = T.grad(cost, params)
        self.i = theano.shared(np.float32(0.))
        i_t = self.i + 1.
        fix1 = 1. - (1. - b1)**i_t
        fix2 = 1. - (1. - b2)**i_t
        lr_t = lr * (T.sqrt(fix2) / fix1)
        for p, g in zip(params, grads):
            self.m = theano.shared(p.get_value() * 0.)
            self.v = theano.shared(p.get_value() * 0.)
            m_t = (b1 * g) + ((1. - b1) * self.m)
            v_t = (b2 * T.sqr(g)) + ((1. - b2) * self.v)
            g_t = m_t / (T.sqrt(v_t) + e)
            p_t = p - (lr_t * g_t)
            updates.append((self.m, m_t))
            updates.append((self.v, v_t))
            updates.append((p, p_t))
        updates.append((self.i, i_t))
        return updates
        
    def train(self,input,target):
        return self.propogate(input,target)
        
    def predict(self,X):
        prediction = self.classify(X)
        return prediction

#train neural network
print "building neural network"
nn = neural_network(convolutional_layers,feature_maps,filter_shapes,feedforward_layers,feedforward_nodes,classes)

batch_size = 100

for i in range(10000):

    #generate 40x40 pixel windows to train on
    input = []
    target = []
    for j in range(batch_size):
    
        #make sure 50% of train batch is composed of positive samples
        if random.random() < .5:
            (a,b,c) = random.choice(nonzero_coords_train)
            X_window = X_train[a,b-20:b+20,c-20:c+20]
            y_window = y_train[a,b-10:b+10,c-10:c+10]
            
            #random horizontal flipping
            if random.random() < .5:
                X_window = X_window[::-1,:]
                y_window = y_window[::-1,:]
                
            #random vertical flipping
            if random.random() < .5:
                X_window = X_window[:,::-1]
                y_window = y_window[:,::-1]
            input.append(X_window)
            target.append(y_window)
            
        #other 50% of train batch is composed of negative samples
        else:
            (a,b,c) = random.choice(zero_coords_train)
            X_window = X_train[a,b-20:b+20,c-20:c+20]
            y_window = y_train[a,b-10:b+10,c-10:c+10]
            
            #random horizontal flipping
            if random.random() < .5:
                X_window = X_window[::-1,:]
                y_window = y_window[::-1,:]
                
            #random vertical flipping
            if random.random() < .5:
                X_window = X_window[:,::-1]
                y_window = y_window[:,::-1]
            input.append(X_window)
            target.append(y_window)
            
    #reshape arrays for conv net
    input = np.array(input).reshape(batch_size,1,40,40)
    target = np.array(target).reshape(len(target),400)

    #print cost
    cost = nn.train(input,target)
    sys.stdout.write("step %i loss: %f \r" % (i+1, cost))
    sys.stdout.flush()
    
#predict on test set
final_output = []

#iterate over each image in test set
for j in range(X_test.shape[0]):
    print 'predicting test image %i of %i' % (j+1, X_test.shape[0])
    
    #create variable to store how many times each pixel has been predicted
    map = np.zeros((512,512))
    
    #create variable to store sum of all predictions over each pixel
    probs = np.zeros((512,512))
    
    #for every pixel in image, predict on 40x40 window around that pixel
    #convnet predicts center 20x20 pixels, so due to overlap each pixel is predicted multiple times
    for x in range(30,522):
        for y in range(30,522):
            sys.stdout.write("analyzing pixel (%i,%i) \r" % (x, y))
            sys.stdout.flush()
            window = X_test[j,x-20:x+20,y-20:y+20].reshape(1,1,40,40)
            pred = nn.predict(window).reshape(20,20)
            probs[x-30:x-10,y-30:y-10]+=pred
            map[x-30:x-10,y-30:y-10]+=1
            
    #final probability per pixel is average probability over all predictions for that pixel
    final = probs/map
    
    #labels for pixels are probabilities rounded to 1 or 0
    labels = np.around(final)
    
    #use scikit label function to separate labels into regions of interest
    final = label(labels)
    
    #convert ROIs to json format
    regions = np.amax(final)
    regionslist = []
    for k in range(1,regions):
        coor = np.argwhere(final==k).tolist()
        dict = {'coordinates':coor}
        regionslist.append(dict)
        
    #get name of dataset
    filename = X_test_files[j].split('.')
    filename = filename[-4]+'.'+filename[-3]+'.'+filename[-2]
    allregions = {"dataset": filename,"regions":regionslist}
    final_output.append(allregions)
    
#save regions to json
with open('submission.json', 'w') as outfile:
    json.dump(final_output, outfile)